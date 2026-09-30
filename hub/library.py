"""One library for everything the four studios have made.

Reads the tools' own output folders and indexes directly (no tool has to be running): Image
Studio's JSON sidecars, Voice Studio's history.json, Lumen's jobs.sqlite, Music Studio's song
folders, and the plain output trees of Forge and ComfyUI (prompts read from the PNG metadata). Files are served through the hub with HTTP range support so audio and video seek.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import THUMBS_DIR

if TYPE_CHECKING:
    from .core import Hub

MODES_TTS = {"custom_voice": "Preset voice", "voice_design": "Voice design", "voice_clone": "Voice clone",
             "longform": "Long-form", "dialogue": "Dialogue", "sts": "Speech to speech", "dub": "Dubbing",
             "tokenizer": "Tokenizer", "batch": "Batch", "edit": "Edited clip", "join": "Joined takes", "mix": "Music mix"}
MODES_IMAGE = {"t2i": "Text to image", "edit": "Edit", "local": "Local edit", "extract": "Extract subject",
               "rgba": "Transparent"}


def _parse_local(s: str) -> float:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return time.mktime(time.strptime(s, fmt))
        except Exception:
            continue
    return 0.0


def _dir_signature(path: Path, depth: int = 1) -> tuple:
    if not path.exists():
        return ("missing",)
    parts: list = [path.stat().st_mtime_ns]
    if depth > 0 and path.is_dir():
        try:
            for child in path.iterdir():
                if child.is_dir():
                    parts.append((child.name, child.stat().st_mtime_ns))
                    if depth > 1:
                        parts.append(_dir_signature(child, depth - 1))
        except OSError:
            pass
    return tuple(parts)


IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXT = {".mp4", ".webm", ".mov", ".gif"}
AUDIO_EXT = {".flac", ".wav", ".mp3"}


def _png_header(path: Path) -> tuple[int | None, int | None, dict[str, str]]:
    """Width, height and the text chunks (tEXt / iTXt / zTXt) of a PNG, read chunk by chunk and stopping
    at the first IDAT - Forge and ComfyUI write their metadata before the pixel data, and decoding a
    1024x1024 image just to read a prompt is what made the first library scan take half a minute."""
    import struct
    import zlib

    w = h = None
    text: dict[str, str] = {}
    try:
        with open(path, "rb") as fh:
            if fh.read(8) != b"\x89PNG\r\n\x1a\n":
                return None, None, {}
            while True:
                head = fh.read(8)
                if len(head) < 8:
                    break
                length, ctype = struct.unpack(">I4s", head)
                if ctype == b"IHDR":
                    data = fh.read(length)
                    w, h = struct.unpack(">II", data[:8])
                elif ctype in (b"tEXt", b"iTXt", b"zTXt") and length < 4 * 1024 * 1024:
                    data = fh.read(length)
                    try:
                        if ctype == b"tEXt":
                            k, v = data.split(b"\x00", 1)
                            text[k.decode("latin-1")] = v.decode("latin-1", "replace")
                        elif ctype == b"zTXt":
                            k, rest = data.split(b"\x00", 1)
                            text[k.decode("latin-1")] = zlib.decompress(rest[1:]).decode("latin-1", "replace")
                        else:
                            k, rest = data.split(b"\x00", 1)
                            comp_flag, _method = rest[0], rest[1]
                            rest = rest[2:]
                            _lang, rest = rest.split(b"\x00", 1)
                            _tkey, payload = rest.split(b"\x00", 1)
                            text[k.decode("latin-1")] = (zlib.decompress(payload) if comp_flag else payload).decode("utf-8", "replace")
                    except Exception:
                        pass
                elif ctype in (b"IDAT", b"IEND"):
                    break
                else:
                    fh.seek(length, 1)
                fh.seek(4, 1)   # CRC
    except OSError:
        return None, None, {}
    return w, h, text


def _image_info(path: Path, want_text: bool) -> tuple[int | None, int | None, dict[str, str]]:
    """(width, height, PNG text chunks) from the file header only - no pixel decoding."""
    if path.suffix.lower() == ".png":
        return _png_header(path)
    try:
        from PIL import Image

        with Image.open(path) as img:
            w, h = img.size
            return w, h, {}
    except Exception:
        return None, None, {}


def _newest_files(root: Path, exts: set[str], limit: int = 600, max_depth: int = 4) -> list[Path]:
    """The newest files below ``root`` (by mtime), without walking the whole tree every time."""
    found: list[tuple[float, Path]] = []
    if not root.is_dir():
        return []
    stack = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        try:
            with __import__("os").scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if depth < max_depth and not e.name.startswith((".", "_")):
                                stack.append((Path(e.path), depth + 1))
                        elif e.is_file(follow_symlinks=False) and Path(e.name).suffix.lower() in exts:
                            found.append((e.stat().st_mtime, Path(e.path)))
                    except OSError:
                        continue
        except OSError:
            continue
    found.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in found[:limit]]


class Library:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self._cache: dict[str, tuple[tuple, float, list[dict[str, Any]]]] = {}

    # ------------------------------------------------------------------ public
    def query(self, tool: str = "", q: str = "", offset: int = 0, limit: int = 60, kind: str = "") -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        tools = [tool] if tool and tool in self.hub.tools else list(self.hub.tools)
        counts: dict[str, int] = {}
        for tid in self.hub.tools:
            got = self._items_for(tid)
            counts[tid] = len(got)
            if tid in tools:
                items.extend(got)
        if kind:
            items = [i for i in items if i["kind"] == kind]
        if q:
            needle = q.lower().strip()
            items = [i for i in items if needle in (i.get("title") or "").lower() or needle in (i.get("subtitle") or "").lower()
                     or needle in (i.get("model") or "").lower()]
        items.sort(key=lambda i: i.get("created") or 0, reverse=True)
        total = len(items)
        return {"items": items[offset:offset + limit], "total": total, "offset": offset, "limit": limit, "counts": counts}

    def media_path(self, tool: str, rel: str) -> Path | None:
        t = self.hub.tools.get(tool)
        if not t:
            return None
        base = t.spec.outputs_dir(t.tool_dir)
        try:
            p = (base / rel).resolve()
        except Exception:
            return None
        if base.resolve() not in p.parents or not p.is_file():
            return None
        return p

    def thumbnail(self, tool: str, rel: str, size: int = 384) -> Path | None:
        """A cached WebP thumbnail for an image that has none of its own."""
        src = self.media_path(tool, rel)
        if not src:
            return None
        key = hashlib.sha1(f"{tool}/{rel}/{src.stat().st_mtime_ns}/{size}".encode("utf-8")).hexdigest()[:24]
        out = THUMBS_DIR / f"{key}.webp"
        if out.is_file():
            return out
        try:
            from PIL import Image

            with Image.open(src) as img:
                img.load()
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA")
                img.thumbnail((size, size), Image.LANCZOS)
                THUMBS_DIR.mkdir(parents=True, exist_ok=True)
                img.save(out, "WEBP", quality=82, method=4)
            return out
        except Exception:
            return None

    # ------------------------------------------------------------------ scanning
    def _items_for(self, tid: str) -> list[dict[str, Any]]:
        t = self.hub.tools[tid]
        outputs = t.spec.outputs_dir(t.tool_dir)
        if tid == "image":
            sig = _dir_signature(outputs, 1)
        elif tid == "tts":
            sig = _dir_signature(outputs / "history.json", 0)
        elif tid == "video":
            sig = _dir_signature(t.tool_dir / "data" / "jobs.sqlite", 0) + _dir_signature(outputs, 0)
        elif tid in ("forge", "comfy"):
            sig = _dir_signature(outputs, 2)
        else:
            sig = _dir_signature(outputs, 1)
        cached = self._cache.get(tid)
        now = time.time()
        if cached and cached[0] == sig and now - cached[1] < 30:
            return cached[2]
        if cached and cached[0] == sig:
            self._cache[tid] = (sig, now, cached[2])
            return cached[2]
        try:
            items = {"image": self._scan_image, "tts": self._scan_tts, "video": self._scan_video,
                     "music": self._scan_music, "forge": self._scan_forge, "comfy": self._scan_comfy}[tid](t.tool_dir, outputs)
        except Exception:
            items = cached[2] if cached else []
        self._cache[tid] = (sig, now, items)
        return items

    def _scan_image(self, tool_dir: Path, outputs: Path) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if not outputs.is_dir():
            return items
        for day in sorted((d for d in outputs.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True)[:120]:
            for js in day.glob("*.json"):
                try:
                    rec = json.loads(js.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(rec, dict) or not rec.get("file"):
                    continue
                file = day / rec["file"]
                if not file.is_file():
                    continue
                base = js.stem
                thumb = day / f"{base}.thumb.webp"
                rel = f"{day.name}/{file.name}"
                w, h = rec.get("width"), rec.get("height")
                mode = MODES_IMAGE.get(str(rec.get("mode", "t2i")), str(rec.get("mode", "")).title())
                if rec.get("rgba") or rec.get("transparent"):
                    mode += " · transparent"
                sub = " · ".join(x for x in (mode, f"{w}×{h}" if w and h else "", f"seed {rec.get('seed')}" if rec.get("seed") is not None else "") if x)
                items.append({
                    "id": f"image:{day.name}/{base}", "tool": "image", "kind": "image",
                    "title": (rec.get("prompt") or rec.get("final_prompt") or base).strip(),
                    "subtitle": sub, "model": "Qwen-Image-2.1", "created": float(rec.get("created") or js.stat().st_mtime),
                    "url": f"/media/image/{rel}", "download": f"/media/image/{rel}?download=1",
                    "thumb": f"/media/image/{day.name}/{thumb.name}" if thumb.is_file() else f"/api/thumb?tool=image&path={rel}",
                    "width": w, "height": h, "size": file.stat().st_size, "steps": rec.get("steps"), "folder": str(day),
                })
        return items

    def _scan_tts(self, tool_dir: Path, outputs: Path) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        hist = outputs / "history.json"
        if not hist.is_file():
            return items
        try:
            entries = json.loads(hist.read_text(encoding="utf-8"))
        except Exception:
            return items
        for e in entries if isinstance(entries, list) else []:
            if not isinstance(e, dict) or not e.get("file"):
                continue
            file = outputs / e["file"]
            if not file.is_file():
                continue
            voice = e.get("speaker") or e.get("voice_id") or ""
            mode = MODES_TTS.get(str(e.get("mode", "")), str(e.get("mode", "")).replace("_", " ").title())
            dur = e.get("duration_s")
            sub = " · ".join(x for x in (mode, str(voice).replace("voice-", "voice "), f"{dur:.1f} s" if isinstance(dur, (int, float)) else "",
                                          e.get("language") or "") if x)
            text = (e.get("text") or e.get("full_text") or "").strip()
            items.append({
                "id": f"tts:{e.get('id') or file.stem}", "tool": "tts", "kind": "audio", "title": text[:300] or file.stem,
                "subtitle": sub, "model": _short(e.get("model") or e.get("engine") or ""),
                "created": _parse_local(str(e.get("created_at", ""))) or file.stat().st_mtime,
                "url": f"/media/tts/{file.name}", "download": f"/media/tts/{file.name}?download=1", "thumb": None,
                "duration": dur, "size": file.stat().st_size, "folder": str(outputs),
            })
        return items

    def _scan_video(self, tool_dir: Path, outputs: Path) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        db = tool_dir / "data" / "jobs.sqlite"
        if not db.is_file():
            return items
        try:
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2)
            try:
                rows = con.execute("SELECT data FROM jobs WHERE status='succeeded' ORDER BY created_at DESC LIMIT 600").fetchall()
            finally:
                con.close()
        except Exception:
            return items
        for (raw,) in rows:
            try:
                j = json.loads(raw)
            except Exception:
                continue
            out = j.get("output_file")
            if not out:
                continue
            file = outputs / out
            if not file.is_file():
                continue
            thumb = outputs / j["thumbnail"] if j.get("thumbnail") else None
            model = {"ltx25": "LTX-2.5", "h3": "MiniMax H3"}.get(j.get("model"), str(j.get("model", "")).upper())
            mode = {"t2v": "Text to video", "i2v": "Image to video", "flf2v": "First + last frame"}.get(j.get("mode"), j.get("mode", ""))
            dur = j.get("duration")
            sub = " · ".join(x for x in (model, mode, f"{j.get('width')}×{j.get('height')}" if j.get("width") else "",
                                          f"{dur:.1f} s" if isinstance(dur, (int, float)) else "", "audio" if j.get("has_audio") else "") if x)
            items.append({
                "id": f"video:{j.get('id') or file.stem}", "tool": "video", "kind": "video",
                "title": (j.get("prompt") or file.stem).strip()[:300], "subtitle": sub, "model": model,
                "created": float(j.get("finished_at") or j.get("created_at") or file.stat().st_mtime),
                "url": f"/media/video/{out}", "download": f"/media/video/{out}?download=1",
                "thumb": f"/media/video/{j['thumbnail']}" if thumb and thumb.is_file() else None,
                "duration": dur, "width": j.get("width"), "height": j.get("height"), "size": file.stat().st_size,
                "folder": str(outputs),
            })
        return items

    def _scan_music(self, tool_dir: Path, outputs: Path) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if not outputs.is_dir():
            return items
        for d in outputs.iterdir():
            if not d.is_dir() or (d / ".deleted").exists():
                continue
            audio = d / "audio.flac"
            result_f, request_f = d / "result.json", d / "request.json"
            if not (audio.is_file() and result_f.is_file()):
                continue
            try:
                result = json.loads(result_f.read_text(encoding="utf-8"))
                request = json.loads(request_f.read_text(encoding="utf-8")) if request_f.is_file() else {}
            except Exception:
                continue
            title_f = d / "title.txt"
            title = title_f.read_text(encoding="utf-8").strip() if title_f.is_file() else d.name
            secs = result.get("audio_seconds")
            style = str(request.get("style") or "")
            versions = [{"name": p.stem.replace("audio-voice-", "voice: "), "url": f"/media/music/{d.name}/{p.name}"}
                        for p in sorted(d.glob("audio-voice-*.flac"))]
            sub = " · ".join(x for x in (style[:90], f"{int(secs // 60)}:{int(secs % 60):02d}" if isinstance(secs, (int, float)) else "") if x)
            items.append({
                "id": f"music:{d.name}", "tool": "music", "kind": "song", "title": title or d.name, "subtitle": sub,
                "model": "YuE2-3B", "created": result_f.stat().st_mtime, "url": f"/media/music/{d.name}/audio.flac",
                "download": f"/media/music/{d.name}/audio.flac?download=1", "thumb": None, "duration": secs,
                "size": audio.stat().st_size, "versions": versions, "has_score": (d / "score.abc").is_file(),
                "lyrics": str(request.get("lyrics") or "")[:2000], "folder": str(d),
            })
        return items


    def _scan_forge(self, tool_dir: Path, outputs: Path) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for file in _newest_files(outputs, IMAGE_EXT | VIDEO_EXT):
            rel = file.relative_to(outputs).as_posix()
            kind = "video" if file.suffix.lower() in VIDEO_EXT else "image"
            w, h, meta = _image_info(file, file.suffix.lower() == ".png") if kind == "image" else (None, None, {})
            params = meta.get("parameters", "")
            prompt = params.split("\nNegative prompt:")[0].split("\nSteps:")[0].strip() if params else ""
            model = ""
            for part in params.split("\n")[-1].split(", ") if params else []:
                if part.startswith("Model: "):
                    model = part[7:]
            folder = file.parent.name
            sub = " · ".join(x for x in (folder.replace("-", " "), f"{w}×{h}" if w and h else "") if x)
            items.append({
                "id": f"forge:{rel}", "tool": "forge", "kind": kind, "title": prompt[:300] or file.stem, "subtitle": sub,
                "model": model or "Forge", "created": file.stat().st_mtime, "url": f"/media/forge/{rel}",
                "download": f"/media/forge/{rel}?download=1",
                "thumb": f"/api/thumb?tool=forge&path={rel}" if kind == "image" else None,
                "width": w, "height": h, "size": file.stat().st_size, "folder": str(file.parent),
            })
        return items

    def _scan_comfy(self, tool_dir: Path, outputs: Path) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for file in _newest_files(outputs, IMAGE_EXT | VIDEO_EXT | AUDIO_EXT):
            rel = file.relative_to(outputs).as_posix()
            ext = file.suffix.lower()
            kind = "video" if ext in VIDEO_EXT else ("audio" if ext in AUDIO_EXT else "image")
            w, h, meta = _image_info(file, ext == ".png") if kind == "image" else (None, None, {})
            prompt = ""
            if meta.get("prompt"):
                try:
                    graph = json.loads(meta["prompt"])
                    texts = [str(n.get("inputs", {}).get("text", "")) for n in graph.values()
                             if isinstance(n, dict) and "text" in (n.get("inputs") or {})]
                    texts = [t.strip() for t in texts if t.strip()]
                    prompt = max(texts, key=len) if texts else ""
                except Exception:
                    prompt = ""
            sub = " · ".join(x for x in (file.parent.name if file.parent != outputs else "", f"{w}×{h}" if w and h else "") if x)
            items.append({
                "id": f"comfy:{rel}", "tool": "comfy", "kind": kind, "title": prompt[:300] or file.stem, "subtitle": sub,
                "model": "ComfyUI", "created": file.stat().st_mtime, "url": f"/media/comfy/{rel}",
                "download": f"/media/comfy/{rel}?download=1",
                "thumb": f"/api/thumb?tool=comfy&path={rel}" if kind == "image" else None,
                "width": w, "height": h, "size": file.stat().st_size, "folder": str(file.parent),
            })
        return items


def _short(model_id: str) -> str:
    return str(model_id).split("/")[-1].replace("Qwen3-TTS-12Hz-", "Qwen3-TTS ")
