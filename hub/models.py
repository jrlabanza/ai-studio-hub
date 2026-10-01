"""The Models page: every studio's models in one place - installed, downloadable, selectable.

Three kinds of studio, one view:

* **folder / file studios** (Image, Music, Forge) keep models on disk; the hub lists the folders and
  files, downloads from HuggingFace, Civitai or a URL straight into the right place (with resume
  and a checksum where the source gives one), verifies, deletes, and tells the studio which one
  to use through its own API.
* **delegating studios** (Voice, Video) already have a model catalogue and downloader of their own;
  the hub shows that catalogue, forwards download / cancel / delete to the studio, and still owns
  "use" so the GPU orchestrator makes room first.

Downloads run in hub threads; progress goes out on the event bus as ``download`` events.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from .config import load_settings

if TYPE_CHECKING:
    from .core import Hub
    from .process import ManagedProcess

CIVITAI_API = "https://civitai.com/api/v1"


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _dir_size(p: Path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(p):
            for f in files:
                try:
                    total += (Path(root) / f).stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _is_extra(rel: str) -> bool:
    """Repository files that are not part of the model (docs, demo assets) - never required."""
    p = Path(rel)
    return (p.name.startswith(".git") or p.suffix.lower() in (".md", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".wav", ".mp3", ".pdf")
            or any(part in ("assets", "examples", "docs", "images", "figures") for part in p.parts[:-1]))


def _safe_name(name: str) -> str:
    name = re.sub(r"[^\w.\- ()+]", "_", name.strip())
    return name.strip(". ") or "model"


# ---------------------------------------------------------------------------------- downloads
class Download:
    def __init__(self, tool: str, kind: str, name: str, source: dict[str, Any], dest: Path) -> None:
        self.id = uuid.uuid4().hex[:10]
        self.tool, self.kind, self.name, self.source, self.dest = tool, kind, name, source, dest
        self.status = "queued"            # queued | running | verifying | done | error | cancelled
        self.message = ""
        self.bytes = 0
        self.total = 0
        self.speed = 0.0
        self.started = 0.0
        self.finished = 0.0
        self.cancel = threading.Event()
        self._last = (0.0, 0)

    @property
    def active(self) -> bool:
        return self.status in ("queued", "running", "verifying")

    def progress(self, done: int, total: int | None = None) -> None:
        now = time.time()
        if total:
            self.total = total
        self.bytes = done
        t0, b0 = self._last
        if now - t0 >= 1.0:
            if t0:
                self.speed = max(0.0, (done - b0) / (now - t0))
            self._last = (now, done)

    def to_dict(self) -> dict[str, Any]:
        eta = (self.total - self.bytes) / self.speed if self.speed > 0 and self.total else None
        return {"id": self.id, "tool": self.tool, "kind": self.kind, "name": self.name, "source": self.source,
                "dest": str(self.dest), "status": self.status, "message": self.message, "bytes": self.bytes,
                "total": self.total, "percent": round(self.bytes / self.total * 100, 1) if self.total else None,
                "speed": round(self.speed), "eta": round(eta) if eta is not None else None,
                "started": self.started, "finished": self.finished}


class CancelledDownload(Exception):
    pass


class Models:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.downloads: dict[str, Download] = {}
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="download")
        self._civitai_cache: dict[str, tuple[float, Any]] = {}

    # ------------------------------------------------------------------ paths
    def host_path(self, tool: "ManagedProcess", path: str) -> Path:
        """A path a studio reports (inside its container on Linux) mapped onto this machine."""
        p = Path(path)
        if tool.is_docker or tool.spec.backend(tool.tool_dir) == "docker":
            s = str(path)
            if s.startswith("/app/") or s == "/app":
                return tool.tool_dir / s[5:]
            if s.startswith("/cache/") or s == "/cache":
                cache = os.environ.get("AI_CACHE") or str(Path.home() / ".cache" / "ai-tools")
                return Path(cache) / s[7:]
        return p

    def kind_dir(self, tool: "ManagedProcess", kind: dict[str, Any]) -> Path:
        return tool.tool_dir / kind["dir"]

    # ------------------------------------------------------------------ listing
    def _scan_kind(self, tool: "ManagedProcess", kind: dict[str, Any]) -> list[dict[str, Any]]:
        base = self.kind_dir(tool, kind)
        items: list[dict[str, Any]] = []
        if not base.is_dir():
            return items
        if kind["layout"] == "folder":
            marker = kind.get("marker")
            for d in sorted(base.iterdir()):
                if not d.is_dir() or d.name.startswith("."):
                    continue
                if marker and not (d / marker).exists():
                    continue
                part = any(p.suffix in (".part", ".incomplete") for p in d.rglob("*.part")) if False else False
                items.append({"name": d.name, "path": str(d), "size": _dir_size(d), "modified": d.stat().st_mtime,
                              "kind": kind["id"], "layout": "folder", "partial": part})
        else:
            exts = tuple(kind.get("exts", ()))
            for f in sorted(base.rglob("*") if kind.get("recursive") else base.iterdir()):
                if not f.is_file() or f.name.startswith(".") or f.name.startswith("put_"):
                    continue
                if exts and f.suffix.lower() not in exts:
                    continue
                rel = f.relative_to(base).as_posix()
                items.append({"name": rel, "path": str(f), "size": f.stat().st_size, "modified": f.stat().st_mtime,
                              "kind": kind["id"], "layout": "file"})
        for it in items:
            it["size_h"] = _fmt_bytes(it["size"])
        return items

    async def snapshot(self) -> dict[str, Any]:
        """Everything the page shows: per studio, its kinds with installed + catalog, plus live downloads."""
        loop = asyncio.get_running_loop()
        studios: list[dict[str, Any]] = []
        for tid in self.hub.order:
            tool = self.hub.tools[tid]
            spec = tool.spec
            entry: dict[str, Any] = {"tool": tid, "name": spec.name, "number": spec.number, "color": spec.color,
                                     "mode": "delegated" if spec.model_delegate else "folders", "running": tool.running,
                                     "kinds": [], "active": {}, "note": spec.model_note}
            if spec.model_delegate:
                entry.update(await self._delegated(tool))
            else:
                for kind in spec.model_kinds:
                    installed = await loop.run_in_executor(None, self._scan_kind, tool, kind)
                    have = {i["name"] for i in installed}
                    catalog = []
                    for c in kind.get("catalog", []):
                        catalog.append({**c, "installed": c["name"] in have})
                    entry["kinds"].append({"id": kind["id"], "label": kind["label"], "dir": str(self.kind_dir(tool, kind)),
                                           "layout": kind["layout"], "sources": kind.get("sources", ["hf", "url"]), "civitai": kind.get("civitai"),
                                           "installed": installed, "catalog": catalog, "selectable": bool(kind.get("use"))})
                entry["active"] = await self._active(tool)
            studios.append(entry)
        return {"studios": studios, "downloads": [d.to_dict() for d in sorted(self.downloads.values(), key=lambda d: d.started or 0)],
                "tokens": {"hf": bool(load_settings().hf_token), "civitai": bool(load_settings().civitai_token)}}

    async def _active(self, tool: "ManagedProcess") -> dict[str, str]:
        """Which installed model each selectable kind is set to, read from the studio when it answers."""
        out: dict[str, str] = {}
        if tool.id == "image":
            try:
                r = await self.hub.http.get(f"{tool.base_url}/api/settings", timeout=3.0) if tool.running else None
                mp = (r.json() if r else {}).get("model_path") or ""
                if not mp:
                    import json
                    mp = (json.loads((tool.tool_dir / "settings.json").read_text(encoding="utf-8")) if (tool.tool_dir / "settings.json").is_file() else {}).get("model_path", "")
                out["model"] = Path(mp).name if mp else "Qwen-Image-2.1"
            except Exception:
                out["model"] = "Qwen-Image-2.1"
        elif tool.id == "forge" and tool.running:
            try:
                r = await self.hub.http.get(f"{tool.base_url}/sdapi/v1/options", timeout=4.0)
                out["checkpoint"] = str(r.json().get("sd_model_checkpoint") or "")
            except Exception:
                pass
        elif tool.id == "music":
            out["model"] = "YuE2-3B"
            if tool.running:
                try:
                    r = await self.hub.http.get(f"{tool.base_url}/api/status", timeout=3.0)
                    out["model"] = str(r.json().get("model_dir") or out["model"])
                except Exception:
                    pass
        return out

    async def _delegated(self, tool: "ManagedProcess") -> dict[str, Any]:
        """The studio's own catalogue, normalised to the page's shape."""
        kinds: list[dict[str, Any]] = []
        active: dict[str, str] = {}
        if not tool.running:
            return {"kinds": kinds, "active": active, "offline": True}
        try:
            if tool.id == "tts":
                r = await self.hub.http.get(f"{tool.base_url}/api/catalog", timeout=6.0)
                cat = r.json()
                st = await self.hub.http.get(f"{tool.base_url}/api/status", timeout=4.0)
                loaded = (st.json() or {}).get("loaded_model") or ""
                items = []
                for m in cat.get("models", []):
                    items.append({"name": m["id"], "label": m.get("label") or m["id"], "installed": bool(m.get("cached")),
                                  "size": int((m.get("cache_mb") or 0) * 1024 * 1024), "size_h": f"{m.get('cache_mb', 0)} MB" if m.get("cached") else f"~{m.get('approx_gb', '?')} GB",
                                  "detail": m.get("desc", ""), "group": m.get("kind", "")})
                tok = cat.get("tokenizer") or {}
                if tok:
                    items.append({"name": tok["id"], "label": "Speech tokenizer", "installed": bool(tok.get("cached")), "size": 0,
                                  "size_h": "", "detail": "Needed by every model", "group": "tokenizer"})
                kinds.append({"id": "model", "label": "Qwen3-TTS models", "layout": "delegated", "items": items,
                              "selectable": True, "dir": cat.get("cache_dir", "")})
                active["model"] = loaded
            elif tool.id == "video":
                r = await self.hub.http.get(f"{tool.base_url}/api/models", timeout=6.0)
                d = r.json()
                dl = d.get("downloads") or {}
                items = []
                for v in d.get("variants", []):
                    files = v.get("files", [])
                    present = all(f.get("present") for f in files) if files else False
                    size = sum(int(f.get("size") or 0) for f in files)
                    items.append({"name": v["id"], "label": v.get("label", v["id"]), "installed": present,
                                  "partial": any(f.get("present") for f in files) and not present, "size": size, "size_h": _fmt_bytes(size),
                                  "detail": v.get("description", ""), "group": v.get("engine", ""),
                                  "files": [{"id": f["id"], "label": f.get("label"), "present": f.get("present"), "size": f.get("size"),
                                             "download": (dl.get(f["id"]) if isinstance(dl, dict) else None)} for f in files]})
                kinds.append({"id": "variant", "label": "Model packs", "layout": "delegated", "items": items, "selectable": True,
                              "dir": d.get("models_dir", "")})
                sel = d.get("selected") or {}
                active.update({f"variant:{k}": v for k, v in sel.items()})
                active["default_model"] = str((await self.hub.http.get(f"{tool.base_url}/api/settings", timeout=4.0)).json().get("default_model", ""))
        except Exception as exc:
            return {"kinds": kinds, "active": active, "offline": True, "error": str(exc)[:160]}
        return {"kinds": kinds, "active": active, "offline": False}

    # ------------------------------------------------------------------ catalogue search
    async def civitai_search(self, query: str, types: str = "Checkpoint", limit: int = 12, base_model: str = "") -> list[dict[str, Any]]:
        params: dict[str, Any] = {"query": query, "limit": max(1, min(limit, 30)), "sort": "Most Downloaded"}
        if types:
            params["types"] = types
        if base_model:
            params["baseModels"] = base_model
        key = repr(sorted(params.items()))
        hit = self._civitai_cache.get(key)
        if hit and time.time() - hit[0] < 300:
            return hit[1]
        headers = {}
        if load_settings().civitai_token:
            headers["Authorization"] = f"Bearer {load_settings().civitai_token}"
        r = await self.hub.http.get(f"{CIVITAI_API}/models", params=params, headers=headers, timeout=20.0)
        r.raise_for_status()
        out = []
        for m in r.json().get("items", []):
            v = (m.get("modelVersions") or [None])[0]
            if not v:
                continue
            f = next((x for x in v.get("files", []) if x.get("primary")), (v.get("files") or [None])[0])
            if not f:
                continue
            out.append({"id": m["id"], "version_id": v["id"], "name": m["name"], "version": v.get("name"), "type": m.get("type"),
                        "base_model": v.get("baseModel"), "nsfw": bool(m.get("nsfw")), "downloads": (m.get("stats") or {}).get("downloadCount"),
                        "file": f.get("name"), "size": int(float(f.get("sizeKB") or 0) * 1024), "size_h": _fmt_bytes(float(f.get("sizeKB") or 0) * 1024),
                        "sha256": ((f.get("hashes") or {}).get("SHA256") or "").lower(), "url": f.get("downloadUrl"),
                        "creator": (m.get("creator") or {}).get("username", ""), "page": f"https://civitai.com/models/{m['id']}"})
        self._civitai_cache[key] = (time.time(), out)
        return out

    async def hf_info(self, repo: str) -> dict[str, Any]:
        from huggingface_hub import HfApi

        def _info():
            api = HfApi(token=load_settings().hf_token or None)
            i = api.model_info(repo, files_metadata=True)
            files = [{"path": s.rfilename, "size": s.size or 0} for s in (i.siblings or [])]
            return {"repo": repo, "sha": i.sha, "gated": bool(getattr(i, "gated", False)), "files": len(files),
                    "size": sum(f["size"] for f in files), "size_h": _fmt_bytes(sum(f["size"] for f in files)),
                    "pipeline": getattr(i, "pipeline_tag", None), "library": getattr(i, "library_name", None),
                    "single_files": [f for f in files if f["path"].endswith((".safetensors", ".ckpt", ".gguf", ".pt", ".pth", ".bin", ".onnx"))][:40]}
        return await asyncio.get_running_loop().run_in_executor(None, _info)

    # ------------------------------------------------------------------ downloads
    def start(self, tool: "ManagedProcess", kind: dict[str, Any] | None, source: dict[str, Any], name: str) -> Download:
        """Queue a download. ``source``: {type: hf|hf_file|url|civitai, repo?, path?, url?, sha256?, allow_patterns?}"""
        stype = source.get("type")
        name = _safe_name(name)
        if kind is None:
            raise ValueError("this studio downloads through its own catalogue")
        base = self.kind_dir(tool, kind)
        if kind["layout"] == "folder":
            dest = base / name
        else:
            dest = base / (name if Path(name).suffix else name + (".safetensors" if stype != "hf" else ""))
        for d in self.downloads.values():
            if d.active and d.dest == dest:
                raise ValueError("already downloading")
        dl = Download(tool.id, kind["id"], name, source, dest)
        self.downloads[dl.id] = dl
        self.pool.submit(self._run, dl)
        return dl

    def _publish(self, dl: Download) -> None:
        self.hub.bus.publish_threadsafe({"type": "download", **dl.to_dict()})

    def _run(self, dl: Download) -> None:
        dl.status, dl.started = "running", time.time()
        self._publish(dl)
        try:
            st = dl.source.get("type")
            if st == "hf":
                self._hf_snapshot(dl)
            elif st == "hf_file":
                self._hf_file(dl)
            elif st in ("url", "civitai"):
                self._http_file(dl)
            else:
                raise ValueError(f"unknown source type {st}")
            if dl.cancel.is_set():
                raise CancelledDownload()
            dl.status, dl.message = "done", f"{dl.name} ready in {dl.dest.parent}"
        except CancelledDownload:
            dl.status, dl.message = "cancelled", "cancelled - partial files are kept and resume next time"
        except Exception as exc:
            dl.status, dl.message = "error", str(exc)[:300]
        dl.finished = time.time()
        self._publish(dl)
        self.hub.bus.log_threadsafe(f"{dl.name}: {dl.message}", tool=dl.tool, level="info" if dl.status == "done" else "warn", source="models")

    def _hf_snapshot(self, dl: Download) -> None:
        from huggingface_hub import HfApi, snapshot_download

        token = load_settings().hf_token or None
        repo = dl.source["repo"]
        patterns = dl.source.get("allow_patterns")
        info = HfApi(token=token).model_info(repo, files_metadata=True)
        wanted = [s for s in (info.siblings or []) if not patterns or any(Path(s.rfilename).match(p) for p in patterns)]
        total = sum((s.size or 0) for s in wanted)
        dl.progress(_dir_size(dl.dest), total)
        stop = threading.Event()

        def watch() -> None:
            while not stop.wait(1.0):
                dl.progress(_dir_size(dl.dest), total)
                self._publish(dl)
                if dl.cancel.is_set():
                    break

        t = threading.Thread(target=watch, daemon=True)
        t.start()
        try:
            from tqdm.auto import tqdm as _tqdm

            class _Tqdm(_tqdm):          # lets "cancel" interrupt the next chunk
                def update(self_, n=1):
                    if dl.cancel.is_set():
                        raise CancelledDownload()
                    return super().update(n)

            snapshot_download(repo, local_dir=str(dl.dest), allow_patterns=patterns, token=token, max_workers=2, tqdm_class=_Tqdm)
        finally:
            stop.set()
        dl.progress(_dir_size(dl.dest), total)

    def _hf_file(self, dl: Download) -> None:
        from huggingface_hub import hf_hub_download

        token = load_settings().hf_token or None
        repo, path = dl.source["repo"], dl.source["path"]
        from huggingface_hub import HfApi

        info = HfApi(token=token).model_info(repo, files_metadata=True)
        total = next((s.size or 0 for s in (info.siblings or []) if s.rfilename == path), 0)
        dl.dest.parent.mkdir(parents=True, exist_ok=True)
        stop = threading.Event()
        tmp_dir = dl.dest.parent / ".hub-download" / dl.id

        def watch() -> None:
            while not stop.wait(1.0):
                dl.progress(_dir_size(tmp_dir), total)
                self._publish(dl)

        t = threading.Thread(target=watch, daemon=True)
        t.start()
        try:
            got = hf_hub_download(repo, path, local_dir=str(tmp_dir), token=token)
        finally:
            stop.set()
        shutil.move(got, dl.dest)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        dl.progress(dl.dest.stat().st_size, total or dl.dest.stat().st_size)

    def _http_file(self, dl: Download) -> None:
        url = dl.source["url"]
        headers: dict[str, str] = {}
        s = load_settings()
        if dl.source.get("type") == "civitai" or "civitai.com" in url:
            if s.civitai_token:
                headers["Authorization"] = f"Bearer {s.civitai_token}"
        elif "huggingface.co" in url and s.hf_token:
            headers["Authorization"] = f"Bearer {s.hf_token}"
        dl.dest.parent.mkdir(parents=True, exist_ok=True)
        part = dl.dest.with_name(dl.dest.name + ".part")
        have = part.stat().st_size if part.is_file() else 0
        if have:
            headers["Range"] = f"bytes={have}-"
        with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0), trust_env=False) as client:
            with client.stream("GET", url, headers=headers) as r:
                if r.status_code == 416:            # already complete
                    have, r_total = have, have
                elif r.status_code == 401 or r.status_code == 403:
                    raise RuntimeError("the server refused the download (401/403): add your Civitai / HuggingFace token in Settings → Models")
                elif r.status_code >= 400:
                    raise RuntimeError(f"HTTP {r.status_code} from the server")
                else:
                    if r.status_code != 206 and have:
                        have = 0            # server ignored the range: start over
                    length = r.headers.get("content-length")
                    total = (int(length) + have) if length else int(dl.source.get("size") or 0)
                    cd = r.headers.get("content-disposition", "")
                    m = re.search(r'filename\*?="?(?:UTF-8\'\')?([^";]+)', cd)
                    if m and not Path(dl.name).suffix:
                        dl.dest = dl.dest.with_name(_safe_name(m.group(1)))
                        part = dl.dest.with_name(dl.dest.name + ".part")
                    dl.progress(have, total)
                    with open(part, "ab" if have else "wb") as fh:
                        for chunk in r.iter_bytes(chunk_size=1024 * 1024):
                            if dl.cancel.is_set():
                                raise CancelledDownload()
                            fh.write(chunk)
                            have += len(chunk)
                            dl.progress(have, total)
                            if int(time.time() * 2) % 2 == 0:
                                self._publish(dl)
        sha = (dl.source.get("sha256") or "").lower()
        if sha:
            dl.status = "verifying"
            self._publish(dl)
            if self._sha256(part, dl) != sha:
                raise RuntimeError("checksum mismatch - the file was removed, try again")
        part.replace(dl.dest)

    @staticmethod
    def _sha256(path: Path, dl: Download | None = None) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
                if dl and dl.cancel.is_set():
                    raise CancelledDownload()
                h.update(chunk)
        return h.hexdigest()

    def cancel(self, dl_id: str) -> bool:
        dl = self.downloads.get(dl_id)
        if not dl or not dl.active:
            return False
        dl.cancel.set()
        return True

    def clear_finished(self) -> None:
        for k in [k for k, d in self.downloads.items() if not d.active]:
            self.downloads.pop(k, None)

    # ------------------------------------------------------------------ delete / verify
    def delete(self, tool: "ManagedProcess", kind: dict[str, Any], name: str) -> Path:
        base = self.kind_dir(tool, kind).resolve()
        target = (base / name).resolve()
        if base not in target.parents or not target.exists():
            raise ValueError("not a model of this studio")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
            part = target.with_name(target.name + ".part")
            if part.exists():
                part.unlink()
        return target

    async def verify(self, tool: "ManagedProcess", kind: dict[str, Any], name: str) -> dict[str, Any]:
        """Folder from HuggingFace: every file present with the right size. Single file: size (and sha256 when known)."""
        base = self.kind_dir(tool, kind)
        target = base / name
        cat = next((c for c in kind.get("catalog", []) if c["name"] == name), None)
        loop = asyncio.get_running_loop()
        if target.is_dir() and cat and cat.get("source", {}).get("type") == "hf":
            from huggingface_hub import HfApi

            def _check():
                info = HfApi(token=load_settings().hf_token or None).model_info(cat["source"]["repo"], files_metadata=True)
                pats = cat["source"].get("allow_patterns")
                missing, wrong = [], []
                for s in info.siblings or []:
                    if pats and not any(Path(s.rfilename).match(p) for p in pats):
                        continue
                    if _is_extra(s.rfilename):
                        continue
                    f = target / s.rfilename
                    if not f.is_file():
                        missing.append(s.rfilename)
                    elif s.size and f.stat().st_size != s.size:
                        wrong.append(s.rfilename)
                return {"ok": not missing and not wrong, "missing": missing[:20], "wrong_size": wrong[:20],
                        "message": "complete" if not missing and not wrong else f"{len(missing)} missing, {len(wrong)} wrong size - redownload to repair"}
            return await loop.run_in_executor(None, _check)
        if target.is_file():
            sha = (cat or {}).get("source", {}).get("sha256") or ""
            if sha:
                got = await loop.run_in_executor(None, self._sha256, target)
                return {"ok": got == sha.lower(), "message": "checksum matches" if got == sha.lower() else "checksum differs - redownload"}
            return {"ok": True, "message": f"{_fmt_bytes(target.stat().st_size)} on disk (no checksum known for this file)"}
        return {"ok": False, "message": "not found"}

    # ------------------------------------------------------------------ use
    async def use(self, tool: "ManagedProcess", kind_id: str, name: str) -> dict[str, Any]:
        """Make a studio switch to a model. Goes through the orchestrator so the GPU is made ready first."""
        orch = self.hub.orchestrator
        if tool.id == "image":
            kind = next(k for k in tool.spec.model_kinds if k["id"] == "model")
            target = self.kind_dir(tool, kind) / name
            if not (target / "model_index.json").is_file():
                raise ValueError("not a complete diffusers model folder (no model_index.json)")
            if not await tool.ensure_running():
                raise RuntimeError(tool.error or "Image Studio could not start")
            # The studio stores an absolute path; give it the path as it sees it (inside the container on Linux).
            path = f"/app/{kind['dir']}/{name}" if tool.is_docker else str(target)
            summ = orch.summary(tool.id)
            if summ.loaded:
                await orch.unload_models(tool, "switching model")
            r = await self.hub.http.post(f"{tool.base_url}/api/settings", json={"model_path": path}, timeout=10.0)
            r.raise_for_status()
            res = await orch.prepare(tool.id, f"load {name}")
            return {"ok": True, "message": f"Image Studio now uses {name}" + (" - loading it" if res.ok else "")}
        if tool.id == "tts":
            if not await tool.ensure_running():
                raise RuntimeError(tool.error or "Voice Studio could not start")
            res = await orch.claim(tool.id, f"load {name}", wait=False)
            if not res.ok:
                raise RuntimeError(res.warning)
            r = await self.hub.http.post(f"{tool.base_url}/api/models/load", json={"model_id": name}, timeout=15.0)
            if r.status_code >= 400:
                raise RuntimeError(r.text[:200])
            return {"ok": True, "message": f"Voice Studio is loading {name.split('/')[-1]} (downloads it first if needed)" + (f" - {res.warning}" if res.warning else "")}
        if tool.id == "forge":
            if not await tool.ensure_running():
                raise RuntimeError(tool.error or "Forge could not start")
            res = await orch.claim(tool.id, f"load {name}", wait=False)
            if not res.ok:
                raise RuntimeError(res.warning)
            r = await self.hub.http.post(f"{tool.base_url}/sdapi/v1/options", json={"sd_model_checkpoint": name}, timeout=600.0)
            if r.status_code >= 400:
                raise RuntimeError(r.text[:200])
            return {"ok": True, "message": f"Forge switched to {name}"}
        if tool.id == "video":
            # name = a variant id ("ltx25-gguf-q4"): becomes that engine's chosen pack and the default engine
            if not await tool.ensure_running():
                raise RuntimeError(tool.error or "Video Studio could not start")
            cat = (await self.hub.http.get(f"{tool.base_url}/api/models", timeout=6.0)).json()
            v = next((v for v in cat.get("variants", []) if v["id"] == name), None)
            if not v:
                raise ValueError("unknown model pack")
            engine = v.get("engine") or name.split("-", 1)[0]
            r = await self.hub.http.put(f"{tool.base_url}/api/settings", json={f"{engine}_variant": name, "default_model": engine}, timeout=10.0)
            r.raise_for_status()
            return {"ok": True, "message": f"Video Studio will use {v.get('label', name)} for {engine} (and {engine} is now the default engine)"}
        if tool.id == "music":
            if not await tool.ensure_running():
                raise RuntimeError(tool.error or "Music Studio could not start")
            r = await self.hub.http.post(f"{tool.base_url}/api/models/select", json={"name": name}, timeout=20.0)
            if r.status_code >= 400:
                raise RuntimeError(r.text[:200])
            d = r.json()
            return {"ok": True, "message": f"Music Studio now uses {name}" + (" - reloading it" if d.get("reloading") else "")}
        raise ValueError("this studio has no selectable models")

    async def delegated_download(self, tool: "ManagedProcess", payload: dict[str, Any]) -> dict[str, Any]:
        if not await tool.ensure_running():
            raise RuntimeError(tool.error or f"{tool.spec.name} could not start")
        if tool.id == "video":
            r = await self.hub.http.post(f"{tool.base_url}/api/models/download", json=payload, timeout=20.0)
        elif tool.id == "tts":
            res = await self.hub.orchestrator.claim(tool.id, "download model", wait=False)
            if not res.ok:
                raise RuntimeError(res.warning)
            r = await self.hub.http.post(f"{tool.base_url}/api/models/load", json={"model_id": payload.get("name")}, timeout=15.0)
        else:
            raise ValueError("not a delegating studio")
        if r.status_code >= 400:
            raise RuntimeError(r.text[:200])
        return {"ok": True}

    async def delegated_action(self, tool: "ManagedProcess", action: str, target: str) -> dict[str, Any]:
        if not tool.running:
            raise RuntimeError(f"{tool.spec.name} is not running")
        if tool.id == "video" and action == "cancel":
            r = await self.hub.http.post(f"{tool.base_url}/api/models/cancel/{target}", timeout=10.0)
        elif tool.id == "video" and action == "delete":
            r = await self.hub.http.delete(f"{tool.base_url}/api/models/file/{target}", timeout=20.0)
        elif tool.id == "tts" and action == "delete":
            r = await self.hub.http.post(f"{tool.base_url}/api/models/delete_cache", json={"model_id": target}, timeout=30.0)
        else:
            raise ValueError("unsupported action")
        if r.status_code >= 400:
            raise RuntimeError(r.text[:200])
        return {"ok": True}
