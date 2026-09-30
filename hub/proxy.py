"""One branded entrance per tool.

Each studio keeps running unchanged on its own loopback port. The hub puts a transparent reverse
proxy in front of it on a port of its own (7901-7904 by default) which

* injects the hub theme and a small bridge script into the tool's HTML,
* asks the orchestrator to make room on the GPU before forwarding a request that will use it,
* starts the tool on demand when it is not running (showing a branded "starting" page meanwhile),
* passes everything else through untouched, including uploads, downloads, server-sent events and
  WebSockets.

Because the proxy serves the tool at the root of its own origin, none of the tools' absolute
``/api/...`` or ``/static/...`` paths need rewriting.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import websockets
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

from . import __version__
from .config import BRAND_DIR, WEB_DIR, load_settings

if TYPE_CHECKING:
    from .core import Hub
    from .process import ManagedProcess

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
              "transfer-encoding", "upgrade"}
# Never forward these: the tools treat forwarded/real-ip headers as "remote visitor" and would demand a login.
STRIP_REQUEST = HOP_BY_HOP | {"host", "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "x-real-ip",
                              "forwarded", "cf-connecting-ip"}
_HEAD_CLOSE = re.compile(r"</head\s*>", re.IGNORECASE)
_HEAD_OPEN = re.compile(r"<head[^>]*>", re.IGNORECASE)


def _safe_child(base: Path, rel: str) -> Path | None:
    try:
        p = (base / rel).resolve()
    except Exception:
        return None
    if base.resolve() not in p.parents or not p.is_file():
        return None
    return p


def build_proxy_app(tool: "ManagedProcess", hub: "Hub") -> FastAPI:
    app = FastAPI(title=f"{tool.spec.name} (hub entrance)", openapi_url=None, docs_url=None, redoc_url=None)
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=None),
                               limits=httpx.Limits(max_connections=200, max_keepalive_connections=20),
                               follow_redirects=False, trust_env=False)
    starting_template = (BRAND_DIR / "starting.html").read_text(encoding="utf-8")

    def hub_url(request: Request) -> str:
        host = request.url.hostname or "127.0.0.1"
        return f"{request.url.scheme}://{host}:{hub.hub_port}"

    def inject(html: str, request: Request) -> str:
        s = load_settings()
        cfg = {"tool": tool.id, "name": tool.spec.name, "short": tool.spec.short, "number": tool.spec.number,
               "color": tool.spec.color, "theme": s.theme, "hub": hub_url(request), "restyle": s.restyle_tools,
               "fonts": s.brand_fonts_in_tools, "version": __version__,
               "claims": [[sorted(methods), prefix] for methods, prefix in tool.spec.claim_routes],
               "exclude": list(tool.spec.claim_exclude_suffixes)}
        block = [f'<script>window.__HUB={json.dumps(cfg)};</script>']
        if s.restyle_tools:
            block.append(f'<link rel="stylesheet" href="/__hub/brand/tools/common.css?v={__version__}">')
            block.append(f'<link rel="stylesheet" href="/__hub/brand/tools/{tool.id}.css?v={__version__}">')
        block.append(f'<script src="/__hub/bridge.js?v={__version__}" defer></script>')
        snippet = "\n".join(block) + "\n"
        if _HEAD_CLOSE.search(html):
            return _HEAD_CLOSE.sub(lambda m: snippet + m.group(0), html, count=1)
        if _HEAD_OPEN.search(html):
            return _HEAD_OPEN.sub(lambda m: m.group(0) + "\n" + snippet, html, count=1)
        return snippet + html

    def starting_page(request: Request) -> HTMLResponse:
        page = starting_template
        for key, val in (("name", tool.spec.name), ("number", tool.spec.number), ("color", tool.spec.color),
                         ("tagline", tool.spec.tagline), ("tool", tool.id), ("theme", load_settings().theme),
                         ("hub", hub_url(request)), ("version", __version__)):
            page = page.replace("{{" + key + "}}", str(val))
        return HTMLResponse(page, status_code=200, headers={"Cache-Control": "no-store"})

    def wants_html(request: Request) -> bool:
        accept = request.headers.get("accept", "")
        return request.method == "GET" and "text/html" in accept

    def ensure_started(force: bool = False) -> None:
        if tool.state not in ("stopped", "error") or not tool.enabled:
            return
        if tool.held and not force and hub.orchestrator.focus != tool.id:
            return          # stopped to free the GPU; a tab polling in the background must not undo that
        asyncio.get_running_loop().create_task(tool.start())

    # ------------------------------------------------------------------ hub-local routes
    @app.get("/__hub/state")
    async def hub_state() -> JSONResponse:
        d = tool.describe()
        d["log"] = tool.tail(14)
        d["summary"] = hub.orchestrator.summary(tool.id).to_dict()
        d["theme"] = load_settings().theme
        d["hub_port"] = hub.hub_port
        return JSONResponse(d, headers={"Cache-Control": "no-store"})

    @app.post("/__hub/start")
    async def hub_start(force: int = 0) -> JSONResponse:
        ensure_started(force=bool(force))
        return JSONResponse({"state": tool.state, "held": tool.held})

    @app.get("/__hub/bridge.js")
    async def bridge_js() -> Response:
        return FileResponse(BRAND_DIR / "bridge.js", media_type="application/javascript",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/__hub/brand/{path:path}")
    async def brand_asset(path: str) -> Response:
        p = _safe_child(BRAND_DIR, path)
        if not p:
            return JSONResponse({"detail": "not found"}, status_code=404)
        return FileResponse(p, headers={"Cache-Control": "public, max-age=3600"})

    # ------------------------------------------------------------------ WebSocket pass-through
    @app.websocket("/{path:path}")
    async def ws_proxy(ws: WebSocket, path: str) -> None:
        if not tool.running:
            await ws.close(code=1013)
            return
        query = ws.url.query
        target = f"ws://127.0.0.1:{tool.port}/{path}" + (f"?{query}" if query else "")
        requested = [p.strip() for p in ws.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
        try:
            upstream = await websockets.connect(target, subprotocols=requested or None, max_size=64 * 1024 * 1024,
                                                open_timeout=15, ping_interval=20, ping_timeout=20)
        except Exception:
            await ws.close(code=1011)
            return
        await ws.accept(subprotocol=upstream.subprotocol)

        async def client_to_upstream() -> None:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if msg.get("text") is not None:
                    await upstream.send(msg["text"])
                elif msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])

        async def upstream_to_client() -> None:
            async for message in upstream:
                if isinstance(message, str):
                    await ws.send_text(message)
                else:
                    await ws.send_bytes(message)

        tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
        except (WebSocketDisconnect, Exception):
            pass
        finally:
            for t in tasks:
                t.cancel()
            try:
                await upstream.close()
            except Exception:
                pass
            try:
                await ws.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ HTTP pass-through
    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def http_proxy(request: Request, path: str) -> Response:
        if not tool.running:
            if not tool.enabled:
                return JSONResponse({"detail": f"{tool.spec.name} is disabled in the hub settings"}, status_code=404)
            ensure_started()
            if wants_html(request):
                return starting_page(request)
            return JSONResponse({"detail": f"{tool.spec.name} is starting - try again in a moment", "hub": "starting",
                                 "state": tool.state}, status_code=503, headers={"Retry-After": "3"})

        method = request.method.upper()
        if tool.spec.is_claim(method, "/" + path):
            res = await hub.orchestrator.claim(tool.id, reason=f"{method} /{path}")
            if not res.ok:
                return JSONResponse({"detail": res.warning or "The GPU could not be reserved"}, status_code=503)
            await hub.orchestrator.before_forward(tool.id, method, "/" + path)

        url = f"http://127.0.0.1:{tool.port}/{path}"
        if request.url.query:
            url += f"?{request.url.query}"
        headers = {k: v for k, v in request.headers.items() if k.lower() not in STRIP_REQUEST}
        headers["host"] = f"127.0.0.1:{tool.port}"
        has_body = method in ("POST", "PUT", "PATCH", "DELETE") or "content-length" in request.headers \
            or "transfer-encoding" in request.headers
        try:
            upstream_req = client.build_request(method, url, headers=headers,
                                                content=request.stream() if has_body else None)
            resp = await client.send(upstream_req, stream=True)
        except httpx.ConnectError:
            if wants_html(request):
                ensure_started()
                return starting_page(request)
            return JSONResponse({"detail": f"{tool.spec.name} is not answering"}, status_code=502)
        except Exception as exc:
            return JSONResponse({"detail": f"proxy error: {exc}"}, status_code=502)

        out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP}
        ctype = resp.headers.get("content-type", "")
        if ctype.startswith("text/html") and resp.status_code == 200 and method == "GET":
            body = await resp.aread()
            await resp.aclose()
            charset = "utf-8"
            m = re.search(r"charset=([\w\-]+)", ctype)
            if m:
                charset = m.group(1)
            try:
                html = body.decode(charset, errors="replace")
            except LookupError:
                html = body.decode("utf-8", errors="replace")
            html = inject(html, request)
            out_headers.pop("content-length", None)
            out_headers.pop("content-encoding", None)
            out_headers["cache-control"] = "no-store"
            return Response(html, status_code=200, headers=out_headers, media_type=ctype.split(";")[0] + "; charset=utf-8")

        async def body_iter():
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            finally:
                await resp.aclose()

        if method == "HEAD" or resp.status_code in (204, 304):
            await resp.aclose()
            return Response(status_code=resp.status_code, headers=out_headers)
        return StreamingResponse(body_iter(), status_code=resp.status_code, headers=out_headers)

    @app.on_event("shutdown")
    async def _close_client() -> None:
        await client.aclose()

    return app
