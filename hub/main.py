"""The hub's own web app: the shell, its JSON API and the live event stream."""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

from . import __version__
from . import docker as dk
from .config import APP_NAME, BRAND_DIR, DEFAULT_TOOLS, PLATFORM, WEB_DIR, load_settings, save_settings
from .core import Hub
from .files import ranged_file
from .process import pick_free_port

def _safe_child(base: Path, rel: str) -> Path | None:
    try:
        p = (base / rel).resolve()
    except Exception:
        return None
    if base.resolve() not in p.parents or not p.is_file():
        return None
    return p


def create_app(hub: Hub) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await hub.start()
        try:
            yield
        finally:
            await hub.stop()

    app = FastAPI(title=APP_NAME, version=__version__, lifespan=lifespan, docs_url="/api/docs", redoc_url=None)

    # ------------------------------------------------------------------ shell
    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8").replace("{{v}}", __version__)
        for key, name in (("mark", "logo-mark.svg"), ("hero", "hero.svg")):
            html = html.replace("{{" + key + "}}", (BRAND_DIR / name).read_text(encoding="utf-8"))
        html = html.replace("{{theme}}", load_settings().theme).replace("{{app}}", APP_NAME)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        return FileResponse(BRAND_DIR / "favicon.svg", media_type="image/svg+xml")

    @app.get("/static/{path:path}")
    async def static(path: str) -> Response:
        p = _safe_child(WEB_DIR, path)
        if not p:
            raise HTTPException(404)
        return FileResponse(p, headers={"Cache-Control": "no-cache"})

    @app.get("/brand/{path:path}")
    async def brand(path: str) -> Response:
        p = _safe_child(BRAND_DIR, path)
        if not p:
            raise HTTPException(404)
        return FileResponse(p, headers={"Cache-Control": "public, max-age=3600"})

    # ------------------------------------------------------------------ state + events
    @app.get("/api/state")
    async def api_state() -> dict[str, Any]:
        return hub.snapshot()

    @app.get("/api/events")
    async def api_events(request: Request) -> StreamingResponse:
        q = hub.bus.subscribe()

        async def gen():
            try:
                yield f"event: state\ndata: {json.dumps(hub.snapshot(), default=str)}\n\n"
                for ev in list(hub.bus.recent)[-40:]:
                    yield f"event: {ev.get('type', 'log')}\ndata: {json.dumps({**ev, 'replay': True}, default=str)}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    payload = ev.get("state") if ev.get("type") == "state" else ev
                    yield f"event: {ev.get('type', 'log')}\ndata: {json.dumps(payload, default=str)}\n\n"
            finally:
                hub.bus.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/activity")
    async def api_activity(limit: int = 80) -> dict[str, Any]:
        return {"events": list(hub.bus.recent)[-max(1, min(limit, 300)):]}

    # ------------------------------------------------------------------ settings
    @app.get("/api/settings")
    async def api_get_settings() -> dict[str, Any]:
        return load_settings().to_dict()

    @app.put("/api/settings")
    async def api_put_settings(request: Request) -> dict[str, Any]:
        data = await request.json()
        if not isinstance(data, dict):
            raise HTTPException(400, "Expected a JSON object")
        if "gpu_policy" in data and data["gpu_policy"] not in ("auto", "exclusive", "budget"):
            raise HTTPException(400, "gpu_policy must be auto, exclusive or budget")
        if "theme" in data and data["theme"] not in ("light", "dark"):
            raise HTTPException(400, "theme must be light or dark")
        s = save_settings(data)
        hub.bus.publish({"type": "settings", "settings": s.to_dict()})
        return s.to_dict()

    # ------------------------------------------------------------------ tools
    def _tool(tool_id: str):
        t = hub.tools.get(tool_id)
        if not t:
            raise HTTPException(404, "Unknown tool")
        return t

    @app.get("/api/tools/{tool_id}")
    async def api_tool(tool_id: str) -> dict[str, Any]:
        t = _tool(tool_id)
        d = t.describe()
        d["summary"] = hub.orchestrator.summary(tool_id).to_dict()
        return d

    @app.post("/api/tools/{tool_id}/start")
    async def api_tool_start(tool_id: str) -> dict[str, Any]:
        t = _tool(tool_id)
        asyncio.create_task(t.start())
        await asyncio.sleep(0.2)
        return {"ok": True, "state": t.state}

    @app.post("/api/tools/{tool_id}/stop")
    async def api_tool_stop(tool_id: str) -> dict[str, Any]:
        t = _tool(tool_id)
        await t.stop("stopped from the hub")
        if hub.orchestrator.owner == tool_id:
            hub.orchestrator.owner = None
        return {"ok": True, "state": t.state}

    @app.post("/api/tools/{tool_id}/restart")
    async def api_tool_restart(tool_id: str) -> dict[str, Any]:
        t = _tool(tool_id)
        asyncio.create_task(t.restart())
        await asyncio.sleep(0.2)
        return {"ok": True, "state": t.state}

    @app.post("/api/tools/{tool_id}/unload")
    async def api_tool_unload(tool_id: str) -> dict[str, Any]:
        t = _tool(tool_id)
        if hub.orchestrator.summary(tool_id).busy:
            raise HTTPException(409, f"{t.spec.name} is busy with a job")
        ok = await hub.orchestrator.unload_models(t, "requested from the hub")
        if hub.orchestrator.owner == tool_id:
            hub.orchestrator.owner = None
        return {"ok": ok}

    @app.post("/api/tools/{tool_id}/prepare")
    async def api_tool_prepare(tool_id: str) -> dict[str, Any]:
        _tool(tool_id)
        res = await hub.orchestrator.prepare(tool_id, "requested from the hub")
        return res.to_dict()

    @app.get("/api/tools/{tool_id}/log")
    async def api_tool_log(tool_id: str, lines: int = 200) -> dict[str, Any]:
        t = _tool(tool_id)
        return {"lines": t.tail(max(1, min(lines, 500))), "state": t.state, "path": str(t.log_path)}

    @app.post("/api/tools/{tool_id}/open-folder")
    async def api_tool_open_folder(tool_id: str) -> dict[str, Any]:
        t = _tool(tool_id)
        folder = t.spec.outputs_dir(t.tool_dir)
        folder.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(folder))  # noqa: S606 - opens Explorer on the user's own PC
        else:
            subprocess.Popen(["xdg-open", str(folder)])
        return {"ok": True, "path": str(folder)}

    # ------------------------------------------------------------------ GPU
    @app.post("/api/gpu/free")
    async def api_gpu_free(request: Request) -> dict[str, Any]:
        body: dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            pass
        actions = await hub.orchestrator.free_gpu(stop_processes=bool(body.get("stop")))
        return {"ok": True, "actions": actions, "free_mb": hub.gpu.latest.free_mb}

    @app.post("/api/focus")
    async def api_focus(request: Request) -> dict[str, Any]:
        body = await request.json()
        tool = body.get("tool") if isinstance(body, dict) else None
        await hub.orchestrator.set_focus(tool if tool in hub.tools else None)
        return {"ok": True, "focus": hub.orchestrator.focus}

    @app.get("/api/gpu/processes")
    async def api_gpu_processes() -> dict[str, Any]:
        info = await hub.gpu.refresh(with_processes=True)
        procs = []
        for p in info.processes:
            owner = next((t.id for t in hub.tools.values() if t.owns_pid(p["pid"])), None)
            procs.append({**p, "tool": owner})
        return {"processes": procs, "used_mb": info.used_mb, "total_mb": info.total_mb}

    # ------------------------------------------------------------------ hand-off ("Send to")
    @app.get("/api/handoff/targets")
    async def api_handoff_targets(kind: str = "") -> dict[str, Any]:
        """Where an output can be sent: every studio's import slots, optionally only those taking ``kind``."""
        kind = {"song": "audio"}.get(kind, kind)
        targets = []
        for tid in hub.order:
            t = hub.tools[tid]
            if not t.enabled:
                continue
            slots = [s for s in t.spec.import_slots if not kind or kind in s["kinds"]]
            if slots:
                targets.append({"tool": tid, "name": t.spec.name, "color": t.spec.color, "number": t.spec.number, "slots": slots})
        return {"targets": targets}

    # ------------------------------------------------------------------ library
    @app.get("/api/library")
    async def api_library(tool: str = "", q: str = "", offset: int = 0, limit: int = 0, kind: str = "") -> dict[str, Any]:
        limit = limit or load_settings().library_page_size
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: hub.library.query(tool, q, max(0, offset), max(1, min(limit, 300)), kind))

    @app.get("/media/{tool}/{path:path}")
    async def media(tool: str, path: str, request: Request, download: int = 0) -> Response:
        p = hub.library.media_path(tool, path)
        if not p:
            raise HTTPException(404)
        return ranged_file(request, p, download=bool(download))

    @app.get("/api/thumb")
    async def api_thumb(tool: str, path: str) -> Response:
        loop = asyncio.get_running_loop()
        p = await loop.run_in_executor(None, hub.library.thumbnail, tool, path)
        if not p:
            raise HTTPException(404)
        return FileResponse(p, media_type="image/webp", headers={"Cache-Control": "private, max-age=86400"})

    @app.post("/api/library/open")
    async def api_library_open(request: Request) -> dict[str, Any]:
        body = await request.json()
        folder = Path(str(body.get("folder", "")))
        allowed = [t.spec.outputs_dir(t.tool_dir).resolve() for t in hub.tools.values()]
        try:
            target = folder.resolve()
        except Exception:
            raise HTTPException(400, "bad folder")
        if not any(target == a or a in target.parents for a in allowed) or not target.is_dir():
            raise HTTPException(400, "folder is not one of the studio output folders")
        if sys.platform == "win32":
            os.startfile(str(target))  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return {"ok": True}

    # ------------------------------------------------------------------ doctor
    @app.get("/api/doctor")
    async def api_doctor() -> dict[str, Any]:
        g = hub.gpu.latest
        vendor = {"nvidia": "NVIDIA", "amd": "AMD"}.get(g.vendor, "")
        checks = [{"name": f"{vendor or 'Graphics'} card", "ok": g.available,
                   "detail": (f"{g.name} · {g.total_mb / 1024:.0f} GB · {g.backend.upper() if g.backend else ''}"
                              f"{' · driver ' + g.driver if g.driver else ''} · read via {g.source}") if g.available
                   else "no NVIDIA or AMD card found (nvidia-smi / rocm-smi / Windows GPU counters all empty)"}]
        if PLATFORM == "linux":
            checks.append({"name": "Docker", "ok": dk.available(),
                           "detail": " ".join(dk.docker() or []) or "not reachable - install Docker + NVIDIA Container Toolkit (any studio's linux/initialize.sh does it)"})
        for t in hub.tools.values():
            tdir = t.tool_dir
            inst, why = t.spec.installed(tdir)
            model_ok, model_why = t.spec.model_present(tdir) if inst else (False, "")
            backend = t.spec.backend(tdir)
            where = f"container image {t.spec.docker_image(tdir)} · {tdir}" if backend == "docker" else str(t.spec.python(tdir))
            checks.append({"name": f"{t.spec.name} environment", "ok": inst, "detail": why or where})
            if inst:
                checks.append({"name": f"{t.spec.name} models", "ok": model_ok, "detail": model_why or "present"})
                prof = t.spec.gpu_profile(tdir)
                if prof:
                    mismatch = bool(g.available and g.vendor and prof.get("vendor") not in (g.vendor, "cpu"))
                    checks.append({"name": f"{t.spec.name} GPU setup", "ok": not mismatch,
                                   "detail": f"set up for {str(prof.get('vendor', '?')).upper()} ({prof.get('backend', '?')}"
                                             f"{', ' + prof['gfx'] if prof.get('gfx') else ''}) · torch {prof.get('torch', '?')}"
                                             + (" - this PC has a different card: re-run the studio's initialiser" if mismatch else "")})
        from .tools import TOOLS

        for tid in hub.absent:
            checks.append({"name": f"{TOOLS[tid].name} (optional)", "ok": True,
                           "detail": f"not found - put a checkout named '{DEFAULT_TOOLS[tid]['dir']}' next to the hub folder to add it"})
        return {"checks": checks, "defaults": DEFAULT_TOOLS, "python": sys.version.split()[0], "platform": PLATFORM}

    return app
