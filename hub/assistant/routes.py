"""/api/assistant/*: plan from a request, render a plan, settings, and the rendered files."""
from __future__ import annotations

import json
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from ..config import load_settings, save_settings
from . import forge, image, music, runtime, video, voice

SESSIONS: dict[str, dict] = {}
STUDIOS = {"forge": {"name": "Forge", "ready": True},
           "video": {"name": "Video Studio", "ready": True}, "image": {"name": "Image Studio", "ready": True},
           "music": {"name": "Music Studio", "ready": True}, "tts": {"name": "Voice Studio", "ready": True}}


def _load(sid: str) -> dict | None:
    """A conversation from disk (sessions survive hub restarts)."""
    if not sid or not sid.isalnum():
        return None
    f = forge.OUT_DIR / sid / "session.json"
    try:
        s = json.loads(f.read_text("utf-8"))
        s["progress"] = None
        SESSIONS[sid] = s
        return s
    except Exception:
        return None


def _session(sid: str | None, studio: str) -> dict:
    if sid and (sid in SESSIONS or _load(sid)):
        return SESSIONS[sid]
    sid = uuid.uuid4().hex[:12]
    SESSIONS[sid] = {"id": sid, "studio": studio, "history": [], "plan": None, "renders": [], "created": time.time(),
                     "progress": None}
    return SESSIONS[sid]


def _last_image(s: dict) -> dict | None:
    """The newest image rendered in this conversation (Forge), for "animate it"."""
    for r in reversed(s["renders"]):
        for url in reversed(r.get("images") or []):
            name = url.rsplit("/", 1)[-1]
            path = forge.OUT_DIR / s["id"] / name
            if path.is_file():
                plan = r.get("plan") or {}
                src = plan.get("checkpoint") or ("Image Studio (Qwen-Image)" if plan.get("studio") == "image" else "Forge")
                return {"path": str(path), "url": url, "label": f"the {src} image from this conversation",
                        "description": plan.get("prompt_core", "")}
    return None


def _save(s: dict) -> None:
    try:
        d = forge.OUT_DIR / s["id"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "session.json").write_text(json.dumps(s, indent=1), "utf-8")
    except Exception:
        pass


def register(app: FastAPI, hub) -> None:
    def port(studio: str) -> int:
        p = hub.proxy_ports.get(studio)
        if not p:
            raise HTTPException(400, f"{studio} has no hub entrance")
        return p

    async def choose_device(override: str | None) -> tuple[str, list[str]]:
        """Auto: plan on the GPU - if a studio is only holding it (loaded but idle), the hub unloads it first and the
        studio reloads for the render; if a studio is rendering, plan on the CPU instead of disturbing it."""
        mode = (override or load_settings().assistant_device or "auto").lower()
        if mode in ("gpu", "cpu"):
            return mode, []
        if runtime.free_vram_mib() >= runtime.GPU_NEEDS_MIB:
            return "gpu", []
        if any(hub.orchestrator.summary(t).busy for t in hub.tools):
            return "cpu", []
        freed = await hub.orchestrator.free_gpu()
        return ("gpu" if runtime.free_vram_mib() >= runtime.GPU_NEEDS_MIB else "cpu"), freed

    @app.get("/api/assistant/state")
    async def state() -> JSONResponse:
        st = load_settings()
        return JSONResponse({"studios": STUDIOS, "device": st.assistant_device, "model": st.assistant_model,
                             "free_vram_mib": runtime.free_vram_mib(), "would_use": runtime.pick_device()})

    @app.put("/api/assistant/settings")
    async def settings(req: Request) -> JSONResponse:
        body = await req.json()
        patch = {}
        if body.get("device") in ("auto", "gpu", "cpu"):
            patch["assistant_device"] = body["device"]
        if isinstance(body.get("model"), str) and body["model"].strip():
            patch["assistant_model"] = body["model"].strip()
        save_settings(patch)
        return await state()

    @app.post("/api/assistant/plan")
    async def plan(req: Request) -> JSONResponse:
        body = await req.json()
        studio = body.get("studio") or "forge"
        if not STUDIOS.get(studio, {}).get("ready"):
            raise HTTPException(400, f"{STUDIOS.get(studio, {}).get('name', studio)} comes later")
        text = " ".join(str(body.get("message") or "").split())
        if not text:
            raise HTTPException(400, "say what to make")
        s = _session(body.get("session"), studio)
        s["studio"] = studio
        prev = s["plan"] if (s["plan"] or {}).get("studio") == studio else None
        try:
            await runtime.ensure_running(load_settings().assistant_model)
            device, freed = await choose_device(body.get("device"))
            if studio == "video":
                p, timing = await video.plan(port(studio), text, s["history"], prev, device, _last_image(s))
            elif studio == "image":
                p, timing = await image.plan(port(studio), text, s["history"], prev, device, _last_image(s))
            elif studio == "music":
                p, timing = await music.plan(port(studio), text, s["history"], prev, device)
            elif studio == "tts":
                p, timing = await voice.plan(port(studio), text, s["history"], prev, device)
            else:
                p, timing = await forge.plan(port(studio), text, s["history"], prev, device)
            timing["freed"] = freed
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, str(e))
        s["history"].append({"role": "user", "text": text})
        s["history"].append({"role": "assistant", "text": p["summary"] or f"a {STUDIOS[studio]['name']} plan"})
        s["plan"] = p
        _save(s)
        return JSONResponse({"session": s["id"], "plan": p, "timing": timing,
                             "final_prompt": forge.final_prompt(p) if studio == "forge" else p.get("prompt", "")})

    @app.post("/api/assistant/render")
    async def render(req: Request) -> JSONResponse:
        body = await req.json()
        s = SESSIONS.get(body.get("session") or "") or _load(body.get("session") or "")
        if not s or not (body.get("plan") or s.get("plan")):
            raise HTTPException(400, "plan first")
        p = {**s["plan"], **(body.get("plan") or {})}      # the plan as edited in the panel
        s["plan"] = p
        def progress(stage, frac):
            s["progress"] = {"stage": stage, "progress": frac, "at": time.time()}
        try:
            await runtime.unload()                           # the planner never sits in VRAM during a render
            if p.get("studio") == "video":
                res = await video.render(port("video"), p, s["id"], progress)
            elif p.get("studio") == "image":
                res = await image.render(port("image"), p, forge.OUT_DIR, s["id"], progress)
            elif p.get("studio") == "music":
                res = await music.render(port("music"), p, s["id"], progress)
            elif p.get("studio") == "tts":
                res = await voice.render(port("tts"), p, s["id"], progress)
            else:
                res = await forge.render(port("forge"), p, s["id"])
        except Exception as e:
            raise HTTPException(500, str(e))
        finally:
            s["progress"] = None
        s["renders"].append({**res, "plan": p, "at": time.time()})
        _save(s)
        return JSONResponse({"session": s["id"], **res})

    @app.get("/api/assistant/progress/{sid}")
    async def progress_of(sid: str) -> JSONResponse:
        s = SESSIONS.get(sid)
        return JSONResponse((s or {}).get("progress") or {})

    @app.get("/api/assistant/file/{sid}/{name}")
    async def file(sid: str, name: str):
        f = forge.OUT_DIR / sid / name
        if "/" in name or ".." in name or not f.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(f)
