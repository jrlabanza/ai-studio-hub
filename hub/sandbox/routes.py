"""The Sandbox's hub API (/api/sandbox/...): projects, draft files, publishing, links and the records.

These routes live on the hub port, next to the rest of the hub API; the uploaded content itself is never served here
(see serve.py - it has a port of its own). Every change takes the author's name from the request body (the hub has
no accounts; the shell remembers the last name used) and the client address from the connection, and lands in the
append-only event log.
"""
from __future__ import annotations

import asyncio
import re
import socket
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from ..config import load_settings
from . import intake
from .intake import IntakeError
from .store import RESERVED, SandboxError, Store

ASSIST_MAX_CHARS = 12000          # the assistant's model has an 8k-token context: the file plus its new version


def lan_ip() -> str:
    """This PC's address on the network (the interface that would route outward; nothing is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return ""


def register(app: FastAPI, hub) -> None:
    store: Store = hub.sandbox

    def limits() -> tuple[int, int]:
        s = load_settings()
        return int(float(s.sandbox_max_mb) * 1048576), int(s.sandbox_max_files)

    def who(request: Request, body: dict | None = None) -> tuple[str, str]:
        author = str((body or {}).get("author") or request.query_params.get("author") or "").strip()
        return author, (request.client.host if request.client else "")

    def bases(request: Request) -> dict[str, Any]:
        """Where links point. share: what other people open (the public URL, or this PC's network address);
        preview: what this browser can reach (the address it opened the hub on)."""
        s = load_settings()
        port = hub.sandbox_port
        here = request.url.hostname or "127.0.0.1"
        local_only = s.bind_host not in ("0.0.0.0", "", "*")
        if s.sandbox_public_url:
            share = s.sandbox_public_url.rstrip("/")
        elif local_only:
            share = f"http://{here}:{port}"
        else:
            host = here if here not in ("127.0.0.1", "localhost", "::1") else (lan_ip() or here)
            share = f"http://{host}:{port}"
        return {"share": share, "preview": f"{request.url.scheme}://{here}:{port}", "port": port,
                "local_only": local_only and not s.sandbox_public_url, "public_url": s.sandbox_public_url}

    async def body_json(request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(400, "Expected a JSON body")
        if not isinstance(data, dict):
            raise HTTPException(400, "Expected a JSON object")
        return data

    async def read_bundle(request: Request) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
        max_bytes, _ = limits()
        cap = max_bytes + intake.MAX_HEADER + 4
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > cap:
            raise HTTPException(413, f"The upload is larger than {max_bytes // 1048576} MB")
        buf = bytearray()
        async for chunk in request.stream():
            buf += chunk
            if len(buf) > cap:
                raise HTTPException(413, f"The upload is larger than {max_bytes // 1048576} MB")
        try:
            return intake.parse_bundle(bytes(buf))
        except IntakeError as exc:
            raise HTTPException(400, str(exc))

    async def run(fn, *args, **kw):
        """Store calls (sqlite + file copies) off the event loop; its refusals become HTTP errors."""
        try:
            return await asyncio.to_thread(fn, *args, **kw)
        except SandboxError as exc:
            raise HTTPException(exc.status, str(exc))
        except IntakeError as exc:
            raise HTTPException(400, str(exc))

    def describe(p: dict[str, Any], request: Request) -> dict[str, Any]:
        b = bases(request)
        files = store.files(p["id"])
        draft = f"{b['preview']}/_draft/{p['id']}/{p['preview_token']}/"
        versions = store.versions(p["id"])
        for v in versions:
            v.pop("manifest", None)
        links = store.links(p["id"])
        return {"project": {k: v for k, v in p.items() if k != "preview_token"}, "draft_url": draft,
                "files": files, "versions": versions, "links": links, "bases": b,
                "size": sum(f["size"] for f in files), "entry": intake.entry_of([f["path"] for f in files]),
                "views": store.views_by_day(p["id"])}

    # ------------------------------------------------------------------ overview
    @app.get("/api/sandbox/state")
    async def sb_state(request: Request) -> dict[str, Any]:
        max_bytes, max_files = limits()
        return {"bases": bases(request), "max_bytes": max_bytes, "max_files": max_files,
                "reserved": sorted(RESERVED), "kinds": list(intake.KINDS)}

    @app.get("/api/sandbox/projects")
    async def sb_projects(request: Request, q: str = "", kind: str = "", status: str = "active") -> dict[str, Any]:
        if status not in ("active", "archived"):
            raise HTTPException(400, "status must be active or archived")
        items = await run(store.list_projects, q.strip(), kind, status)
        return {"projects": items, "bases": bases(request)}

    @app.get("/api/sandbox/events")
    async def sb_events(limit: int = 100) -> dict[str, Any]:
        return {"events": await run(store.events, None, max(1, min(limit, 500)))}

    @app.get("/api/sandbox/slug")
    async def sb_slug(slug: str = "") -> dict[str, Any]:
        return await run(store.slug_status, slug)

    # ------------------------------------------------------------------ projects
    @app.post("/api/sandbox/projects")
    async def sb_create(request: Request) -> dict[str, Any]:
        head, pairs = await read_bundle(request)
        max_bytes, max_files = limits()
        files = await run(intake.intake, pairs, max_bytes, max_files, True) if pairs else {}
        author, ip = who(request, head)
        p = await run(store.create, head.get("name", ""), head.get("description", ""), author, files, ip, max_bytes)
        return describe(p, request)

    @app.get("/api/sandbox/projects/{pid}")
    async def sb_project(pid: str, request: Request) -> dict[str, Any]:
        p = await run(store.project, pid)
        return describe(p, request)

    @app.patch("/api/sandbox/projects/{pid}")
    async def sb_update(pid: str, request: Request) -> dict[str, Any]:
        body = await body_json(request)
        author, ip = who(request, body)
        patch = {k: body[k] for k in ("name", "description", "kind", "allow_storage") if k in body}
        p = await run(store.update, pid, patch, author, ip)
        return describe(p, request)

    @app.post("/api/sandbox/projects/{pid}/archive")
    async def sb_archive(pid: str, request: Request) -> dict[str, Any]:
        body = await body_json(request)
        author, ip = who(request, body)
        p = await run(store.set_status, pid, "archived" if body.get("archived", True) else "active", author, ip)
        return describe(p, request)

    @app.delete("/api/sandbox/projects/{pid}")
    async def sb_delete(pid: str, request: Request) -> dict[str, Any]:
        body = await body_json(request)
        author, ip = who(request, body)
        if body.get("confirm") != pid:
            raise HTTPException(400, "Confirm the deletion (confirm must be the project id)")
        await run(store.delete, pid, author, ip)
        return {"ok": True}

    @app.post("/api/sandbox/projects/{pid}/preview-token")
    async def sb_rotate(pid: str, request: Request) -> dict[str, Any]:
        body = await body_json(request)
        author, ip = who(request, body)
        await run(store.rotate_token, pid, author, ip)
        return describe(await run(store.project, pid), request)

    @app.get("/api/sandbox/projects/{pid}/events")
    async def sb_project_events(pid: str, limit: int = 300) -> dict[str, Any]:
        await run(store.project, pid, allow_deleted=True)
        return {"events": await run(store.events, pid, max(1, min(limit, 1000)))}

    # ------------------------------------------------------------------ draft files
    @app.post("/api/sandbox/projects/{pid}/upload")
    async def sb_upload(pid: str, request: Request) -> dict[str, Any]:
        await run(store.project, pid)
        head, pairs = await read_bundle(request)
        max_bytes, max_files = limits()
        files = await run(intake.intake, pairs, max_bytes, max_files, False)
        author, ip = who(request, head)
        res = await run(store.add_files, pid, files, author, ip, max_bytes, max_files)
        return {**describe(res["project"], request), "added": res["added"], "replaced": res["replaced"]}

    @app.get("/api/sandbox/projects/{pid}/file")
    async def sb_file(pid: str, path: str) -> dict[str, Any]:
        fp, data = await run(store.read_file, pid, path)
        text = intake.is_text(path, data) and len(data) <= 2 * 1024 * 1024
        return {"path": fp.relative_to(store.draft_dir(pid)).as_posix(), "size": len(data), "text": text,
                "content": data.decode("utf-8", errors="replace") if text else None}

    @app.put("/api/sandbox/projects/{pid}/file")
    async def sb_save(pid: str, request: Request) -> dict[str, Any]:
        body = await body_json(request)
        author, ip = who(request, body)
        if not isinstance(body.get("content"), str):
            raise HTTPException(400, "content must be text")
        max_bytes, max_files = limits()
        res = await run(store.save_text, pid, str(body.get("path", "")), body["content"], author, ip,
                        bool(body.get("create")), max_bytes, max_files)
        return {**describe(res["project"], request), "path": res["path"], "changed": res["changed"]}

    @app.post("/api/sandbox/projects/{pid}/rename")
    async def sb_rename(pid: str, request: Request) -> dict[str, Any]:
        body = await body_json(request)
        author, ip = who(request, body)
        res = await run(store.rename_file, pid, str(body.get("from", "")), str(body.get("to", "")), author, ip)
        return {**describe(res["project"], request), "path": res["path"]}

    @app.delete("/api/sandbox/projects/{pid}/file")
    async def sb_delete_file(pid: str, request: Request, path: str) -> dict[str, Any]:
        author, ip = who(request)
        res = await run(store.delete_file, pid, path, author, ip)
        return describe(res["project"], request)

    @app.get("/api/sandbox/projects/{pid}/zip")
    async def sb_zip(pid: str, version: int = 0) -> Response:
        p = await run(store.project, pid)
        top = re.sub(r"[^A-Za-z0-9._-]+", "-", p["name"]).strip("-") or "project"
        if version:
            v = await run(store.version, pid, version)
            if not v:
                raise HTTPException(404, "No such version")
            base, paths, name = store.version_dir(pid, version), [f["path"] for f in v["manifest"]], f"{top}-v{version}"
        else:
            base, paths, name = store.draft_dir(pid), [f["path"] for f in store.files(pid)], f"{top}-draft"
        data = await asyncio.to_thread(store.zip_bytes, base, paths, name)
        return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})

    # ------------------------------------------------------------------ publish, versions, links
    @app.post("/api/sandbox/projects/{pid}/publish")
    async def sb_publish(pid: str, request: Request) -> dict[str, Any]:
        body = await body_json(request)
        author, ip = who(request, body)
        v = await run(store.publish, pid, str(body.get("note") or ""), author, ip, str(body.get("slug") or "") or None,
                      bool(body.get("pin")))
        v.pop("manifest", None)
        return {**describe(await run(store.project, pid), request), "published": v}

    @app.get("/api/sandbox/projects/{pid}/versions/{number}")
    async def sb_version(pid: str, number: int) -> dict[str, Any]:
        await run(store.project, pid)
        v = await run(store.version, pid, number)
        if not v:
            raise HTTPException(404, "No such version")
        return v

    @app.post("/api/sandbox/projects/{pid}/versions/{number}/verify")
    async def sb_verify(pid: str, number: int) -> dict[str, Any]:
        await run(store.project, pid)
        return await run(store.verify, pid, number)

    @app.post("/api/sandbox/projects/{pid}/links")
    async def sb_link_create(pid: str, request: Request) -> dict[str, Any]:
        body = await body_json(request)
        author, ip = who(request, body)
        await run(store.create_link, pid, str(body.get("slug", "")), body.get("target"), author, ip)
        return describe(await run(store.project, pid), request)

    @app.patch("/api/sandbox/links/{slug}")
    async def sb_link_repoint(slug: str, request: Request) -> dict[str, Any]:
        body = await body_json(request)
        author, ip = who(request, body)
        l = await run(store.repoint, slug, body.get("target"), author, ip)
        return describe(await run(store.project, l["project_id"]), request)

    @app.post("/api/sandbox/links/{slug}/retire")
    async def sb_link_retire(slug: str, request: Request) -> dict[str, Any]:
        body = await body_json(request)
        author, ip = who(request, body)
        l = await run(store.retire, slug, author, ip)
        return describe(await run(store.project, l["project_id"]), request)

    # ------------------------------------------------------------------ "Ask the Assistant to change this file"
    @app.post("/api/sandbox/projects/{pid}/assist")
    async def sb_assist(pid: str, request: Request) -> dict[str, Any]:
        """The hub Assistant's local model rewrites one file as asked. Nothing is saved: the shell shows the change
        and the user applies it to the editor (then saves as usual). Runs on the GPU only when it is free - it never
        pushes a studio out (otherwise on the CPU, slower)."""
        from ..assistant import runtime

        body = await body_json(request)
        await run(store._writable, pid)
        path = str(body.get("path") or "")
        text = body.get("content")
        ask = " ".join(str(body.get("instruction") or "").split())[:1000]
        if not isinstance(text, str) or not path:
            raise HTTPException(400, "path and content are needed")
        if not ask:
            raise HTTPException(400, "Say what to change")
        if len(text) > ASSIST_MAX_CHARS:
            raise HTTPException(413, f"The assistant handles files up to {ASSIST_MAX_CHARS // 1000} k characters")
        device = "cpu" if body.get("device") == "cpu" else runtime.pick_device("auto")
        messages = [
            {"role": "system", "content": "You edit one file of a small project. Apply exactly the requested change and "
             "keep everything else as it is. Reply with the complete new content of the file only: no explanations, "
             "no markdown fences."},
            {"role": "user", "content": f"File: {path}\nChange: {ask}\n\n----- current content -----\n{text}"}]
        try:
            await runtime.ensure_running(load_settings().assistant_model)
            out, info = await runtime.chat(messages, device=device, temperature=0.2)
        except Exception as exc:
            raise HTTPException(502, f"The assistant's model is not available: {exc}")
        finally:
            await runtime.unload()
        out = out.strip("\n")
        m = re.fullmatch(r"\s*```[\w+-]*\n(.*?)\n?```\s*", out, re.S)
        if m:
            out = m.group(1)
        if text.endswith("\n") and not out.endswith("\n"):
            out += "\n"
        author, ip = who(request, body)
        p = await run(store.project, pid)
        store.event("assistant suggested a change", author, ip, p, path=path, instruction=ask[:200],
                    device=info.get("device"), seconds=info.get("seconds"))
        return {"content": out, "info": info}
