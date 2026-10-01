"""Everything the hub knows about each tool.

For every studio: how to launch it in its own environment, how to tell that it is healthy, which
requests use the GPU (these are the *claim routes* the orchestrator intercepts), how to make it
let go of the GPU, how to warm it up, and how much VRAM it wants on a given card.

The tools are used exactly as their own launchers use them (same interpreter, same arguments,
same environment variables); nothing inside their folders is edited, except Lumen's
``engine_autostart`` flag which would otherwise grab the GPU the moment the backend starts.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import docker as dk

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
DEFAULT_TTS_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def repair_editable_installs(python: Path, tool_dir: Path, sources: dict[str, str]) -> list[str]:
    """Re-link "pip install -e" packages whose recorded source folder no longer exists.

    An editable install records the absolute path of its source tree. When a whole tool folder is
    moved (for example into this hub's folder) that path goes stale and the tool dies at start-up
    with ``ModuleNotFoundError``. This re-runs ``pip install -e <local source> --no-deps`` for each
    affected package, which only rewrites the link and touches nothing else.
    """
    notes: list[str] = []
    site = python.parent.parent / "Lib" / "site-packages"
    if not site.is_dir():
        return notes

    def relocate(old: str, src: Path) -> Path | None:
        """Map an old absolute path onto the moved source folder by its trailing path segments."""
        parts = Path(old).parts
        anchor = src.name.lower()
        for i in range(len(parts) - 1, -1, -1):
            if parts[i].lower() == anchor:
                cand = src.joinpath(*parts[i + 1:])
                return cand if cand.exists() else None
        return None

    for pkg, rel in sources.items():
        src = tool_dir / rel
        if not src.is_dir():
            continue
        key = pkg.replace("-", "_").lower()
        stale_files: list[tuple[Path, str, list[str]]] = []
        for f in site.glob("__editable__*"):
            if key not in f.name.replace("-", "_").lower():
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if f.suffix == ".pth":
                paths = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("import")]
            else:
                paths = [m.group(1).replace("\\\\", "\\") for m in re.finditer(r"'([A-Za-z]:\\\\[^']*|/[^']*)'", text)]
            stale = [p for p in paths if not Path(p).exists()]
            if stale:
                stale_files.append((f, text, stale))
        if not stale_files:
            continue
        notes.append(f"the editable install of '{pkg}' points at a moved folder - re-linking it to {src}")
        # 1. Rewrite the recorded paths in place (no pip, no network, nothing else touched).
        rewritten = True
        for f, text, stale in stale_files:
            new_text = text
            for old in stale:
                new = relocate(old, src)
                if new is None:
                    rewritten = False
                    break
                if f.suffix == ".pth":
                    new_text = new_text.replace(old, str(new))
                else:
                    new_text = new_text.replace(old.replace("\\", "\\\\"), str(new).replace("\\", "\\\\"))
            if not rewritten:
                break
            try:
                f.write_text(new_text, encoding="utf-8")
            except Exception as exc:
                notes.append(f"could not rewrite {f.name}: {exc}")
                rewritten = False
                break
        if rewritten:
            notes.append(f"'{pkg}' re-linked")
            continue
        # 2. Fall back to pip (installing pip into the environment first if it has none).
        try:
            probe = subprocess.run([str(python), "-m", "pip", "--version"], capture_output=True, text=True, timeout=120,
                                   creationflags=_CREATE_NO_WINDOW)
            if probe.returncode != 0:
                subprocess.run([str(python), "-m", "ensurepip", "--upgrade"], capture_output=True, text=True, timeout=300,
                               creationflags=_CREATE_NO_WINDOW)
            out = subprocess.run([str(python), "-m", "pip", "install", "-e", str(src), "--no-deps", "--quiet",
                                  "--disable-pip-version-check"], cwd=str(tool_dir), capture_output=True, text=True,
                                 timeout=900, creationflags=_CREATE_NO_WINDOW)
            if out.returncode != 0:
                notes.append(f"re-linking '{pkg}' failed: {(out.stderr or out.stdout).strip()[-400:]}")
            else:
                notes.append(f"'{pkg}' re-linked with pip")
        except Exception as exc:
            notes.append(f"re-linking '{pkg}' failed: {exc}")
    return notes


@dataclass
class Step:
    """One HTTP call the orchestrator performs on a tool."""
    method: str
    url: str
    json: Any = None
    timeout: float = 30.0
    label: str = ""
    fire_and_forget: bool = False   # do not wait for long-running handlers (engine start)


@dataclass
class Helper:
    """An extra process a tool needs (the Chatterbox clone engine of Voice Studio)."""
    name: str
    argv: list[str]
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Summary:
    """What a tool says about itself, normalised so the shell can show all four alike."""
    state: str = "unknown"          # unloaded | loading | ready | busy | error | unknown
    loaded: bool = False            # holds (or is loading) a model on the GPU
    busy: bool = False              # a job is running or queued
    label: str = ""                 # short line, e.g. "Model ready - Minimal VRAM profile"
    detail: str = ""                # second line
    model: str = ""                 # model name if known
    job: dict[str, Any] | None = None   # {"title", "percent", "eta", "message", "kind"}
    gpu_used_mb: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "loaded": self.loaded, "busy": self.busy, "label": self.label,
                "detail": self.detail, "model": self.model, "job": self.job, "gpu_used_mb": self.gpu_used_mb}


def _short_model(model_id: str | None) -> str:
    if not model_id:
        return ""
    name = str(model_id).split("/")[-1]
    return name.replace("Qwen3-TTS-12Hz-", "Qwen3-TTS ")


def _pct(v: Any) -> float | None:
    try:
        return None if v is None else max(0.0, min(100.0, float(v)))
    except (TypeError, ValueError):
        return None


BASE_ENV = {"PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "HUB_MANAGED": "1"}


class ToolSpec:
    id = ""
    name = ""
    short = ""
    tagline = ""
    number = "00"
    color = "#8A8A93"
    icon = "spark"
    health_path = "/"
    status_path = "/"
    startup_timeout = 120.0
    restart_exit_codes: frozenset[int] = frozenset()
    port_regex: str | None = None
    outputs_rel = "outputs"
    claim_routes: tuple[tuple[frozenset[str], str], ...] = ()
    claim_exclude_suffixes: tuple[str, ...] = ()
    python_rel = ".venv/Scripts/python.exe" if sys.platform == "win32" else ".venv/bin/python"
    idle_context_mb = 400          # VRAM a running-but-unloaded process still holds (CUDA context)
    editable_sources: dict[str, str] = {}   # package -> source folder (relative) installed with pip -e
    checkout_markers: tuple[str, ...] = ()  # files that identify a checkout of this tool's repository
    # Linux packaging: every studio ships linux/compose.yml with one service (its ``ai.tool`` label)
    # and a fixed port. The hub drives that container; see docker.py.
    docker_service = ""
    docker_port = 0
    docker_extra_services: tuple[str, ...] = ()   # optional helpers, started with their compose profile
    supports_unload = True          # False: the tool only holds the GPU while a job runs (Music Studio)
    # "Send to" targets: what this studio can take from another one. Each slot is delivered to the
    # studio's page through the hub bridge (window.hubImport) - see docs/handoff.md.
    import_slots: tuple[dict[str, Any], ...] = ()
    # Models page (hub/models.py): where this studio keeps models, what the hub can download for it,
    # and whether the studio has its own catalogue the hub delegates to instead.
    model_kinds: tuple[dict[str, Any], ...] = ()
    model_delegate = False
    model_note = ""

    # ------------------------------------------------------------------ installation
    def python(self, tool_dir: Path) -> Path:
        return tool_dir / self.python_rel

    @staticmethod
    def gpu_profile(tool_dir: Path) -> dict[str, Any]:
        """What the studio's initialiser set it up for (docs/gpu.md): ``{"vendor","backend","gfx",...}``.
        Empty when the studio was set up before the vendor-aware initialisers (treated as NVIDIA)."""
        try:
            d = json.loads((tool_dir / ".gpu.json").read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def is_checkout(self, tool_dir: Path) -> bool:
        return tool_dir.is_dir() and all((tool_dir / m).exists() for m in self.checkout_markers)

    def docker_backend(self, tool_dir: Path) -> str:
        """``rocm`` when the studio was set up for an AMD card on Linux (and the ROCm packaging exists), else ``cuda``."""
        prof = self.gpu_profile(tool_dir)
        if prof.get("backend") == "rocm" and (tool_dir / "linux" / "compose.rocm.yml").is_file():
            return "rocm"
        return "cuda"

    def compose_file(self, tool_dir: Path) -> Path:
        if self.docker_backend(tool_dir) == "rocm":
            return tool_dir / "linux" / "compose.rocm.yml"
        return tool_dir / "linux" / "compose.yml"

    def docker_image(self, tool_dir: Path) -> str:
        return f"ai/{self.docker_service}:{'rocm' if self.docker_backend(tool_dir) == 'rocm' else 'latest'}"

    def container_name(self) -> str:
        return f"ai-{self.docker_service}"

    def compose_overrides(self, tool_dir: Path) -> tuple[Path, ...]:
        """The hub's own additions to the studio's compose file (hub/compose/<service>.yml), if any."""
        f = Path(__file__).resolve().parent / "compose" / f"{self.docker_service}.yml"
        return (f,) if f.is_file() else ()

    def backend(self, tool_dir: Path) -> str:
        """``native`` (the tool's own Python environment), ``docker`` (its Linux container image) or ``""``."""
        if self.python(tool_dir).is_file():
            return "native"
        if IS_LINUX and self.docker_service and self.compose_file(tool_dir).is_file():
            return "docker"
        return ""

    def installed(self, tool_dir: Path) -> tuple[bool, str]:
        if not tool_dir.is_dir():
            return False, f"Folder not found: {tool_dir}"
        backend = self.backend(tool_dir)
        if backend == "native":
            return True, ""
        if backend == "docker":
            if not dk.available():
                return False, "Docker is not reachable - is it installed and are you in the docker group? (linux/initialize.sh sets it up)"
            if not dk.image_exists(self.docker_image(tool_dir)):
                return False, f"Container image {self.docker_image(tool_dir)} not built yet - run linux/initialize.sh in {tool_dir.name} (or: ai init {self.docker_service})"
            return True, ""
        if IS_LINUX:
            return False, "Not set up yet - run linux/initialize.sh in the tool folder once (or: ai init <tool>)"
        return False, "Not set up yet - run this tool's Initialize script once"

    def docker_services(self, tool_dir: Path, cfg: dict[str, Any]) -> tuple[list[str], tuple[str, ...]]:
        """(compose services to bring up, compose profiles to enable)."""
        return [self.docker_service], ()

    def docker_env(self, tool_dir: Path, cfg: dict[str, Any]) -> dict[str, str]:
        return {}

    def resolved_port(self, tool_dir: Path, cfg: dict[str, Any]) -> int:
        """The backend port. Containers listen on the port their compose file fixes; native tools take
        whatever the hub passes on the command line."""
        if self.backend(tool_dir) == "docker":
            info = dk.read_run_sh(tool_dir / "linux")
            return int(info.get("port") or self.docker_port or cfg.get("port") or 0)
        return int(cfg.get("port") or self.docker_port or 0)

    def model_present(self, tool_dir: Path) -> tuple[bool, str]:
        return True, ""

    def outputs_dir(self, tool_dir: Path) -> Path:
        return tool_dir / self.outputs_rel

    # ------------------------------------------------------------------ launching
    def argv(self, tool_dir: Path, port: int, cfg: dict[str, Any]) -> list[str]:
        raise NotImplementedError

    def env(self, tool_dir: Path, cfg: dict[str, Any]) -> dict[str, str]:
        return dict(BASE_ENV)

    def helpers(self, tool_dir: Path, cfg: dict[str, Any]) -> list[Helper]:
        return []

    def pre_launch(self, tool_dir: Path, cfg: dict[str, Any]) -> list[str]:
        """Runs right before the tool starts; returns notes for the tool's log."""
        if self.editable_sources and self.python(tool_dir).is_file():
            return repair_editable_installs(self.python(tool_dir), tool_dir, self.editable_sources)
        return []

    # ------------------------------------------------------------------ health / status
    def health_ok(self, status_code: int, body: Any) -> bool:
        return status_code == 200

    def status_requests(self, base: str, body: dict[str, Any]) -> list[tuple[str, str]]:
        """Extra GET requests whose JSON is handed to parse_status() under the given keys."""
        return []

    def parse_status(self, body: dict[str, Any], extra: dict[str, Any]) -> Summary:
        return Summary()

    # ------------------------------------------------------------------ GPU handling
    def unload_steps(self, base: str, cfg: dict[str, Any], tool_dir: Path) -> list[Step]:
        return []

    def prepare_steps(self, base: str, cfg: dict[str, Any], tool_dir: Path) -> list[Step]:
        return []

    def pre_forward_steps(self, base: str, method: str, path: str, summary: "Summary") -> list[Step]:
        """Calls to make right before a claimed request is forwarded (e.g. queue a model load)."""
        return []

    def vram_need_gb(self, total_gb: float) -> float:
        return 4.0

    def is_claim(self, method: str, path: str) -> bool:
        if not path.startswith("/"):
            path = "/" + path
        for methods, prefix in self.claim_routes:
            if method.upper() in methods and path.startswith(prefix):
                if any(path.endswith(s) for s in self.claim_exclude_suffixes):
                    return False
                return True
        return False

    def describe(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "short": self.short, "tagline": self.tagline,
                "number": self.number, "color": self.color, "icon": self.icon, "import_slots": list(self.import_slots)}


POST = frozenset({"POST"})
GET_POST = frozenset({"GET", "POST"})


# ======================================================================= 01 Image Studio
class ImageTool(ToolSpec):
    id = "image"
    name = "Image Studio"
    short = "Image"
    tagline = "Qwen-Image-2.1 · text to image, multi-reference editing, transparent layers"
    number = "01"
    color = "#F4C15D"
    icon = "image"
    health_path = "/api/status"
    status_path = "/api/status"
    startup_timeout = 300.0                 # importing torch + diffusers on a cold disk cache is slow
    restart_exit_codes = frozenset({42})    # "GPU context lost" - the tool asks to be restarted
    port_regex = r"UI:\s+http://[\w.\-]+:(\d+)"
    claim_routes = ((POST, "/api/generate"), (POST, "/api/enhance"), (POST, "/api/model/load"))
    checkout_markers = ("server/main.py",)
    docker_service = "qwen-image"
    docker_port = 7864
    model_kinds = (
        {"id": "model", "label": "Models (diffusers folders)", "dir": "models", "layout": "folder", "marker": "model_index.json",
         "use": True, "sources": ["hf"], "catalog": [
             {"name": "Qwen-Image-2.1", "label": "Qwen-Image-2.1", "detail": "Text to image + editing, the main model", "size_h": "~33 GB",
              "source": {"type": "hf", "repo": "Qwen/Qwen-Image-2.1"}},
             {"name": "Qwen-Image-2.1-PE-T2I", "label": "Prompt enhancer (text to image)", "detail": "Powers the Enhance button for text-to-image", "size_h": "~19 GB",
              "source": {"type": "hf", "repo": "Qwen/Qwen-Image-2.1-PE-T2I"}},
             {"name": "Qwen-Image-2.1-PE-I2I", "label": "Prompt enhancer (editing)", "detail": "Powers the Enhance button when editing", "size_h": "~19 GB",
              "source": {"type": "hf", "repo": "Qwen/Qwen-Image-2.1-PE-I2I"}}]},
        {"id": "lora", "label": "LoRAs", "dir": "loras", "layout": "file", "exts": [".safetensors"], "sources": ["civitai", "url", "hf_file"],
         "civitai": {"types": "LORA", "base": "Qwen"}},
    )
    model_note = "Any Qwen-Image diffusers checkpoint works as a model folder; a different architecture needs a code update."

    import_slots = (
        {'id': 'edit', 'label': 'Edit & Combine - as a reference image', 'kinds': ['image']},
        {'id': 'local', 'label': 'Local edit - as the image to edit', 'kinds': ['image']},
        {'id': 'extract', 'label': 'Extract subject', 'kinds': ['image']},
    )

    def model_present(self, tool_dir: Path) -> tuple[bool, str]:
        ok = (tool_dir / "models" / "Qwen-Image-2.1" / "model_index.json").is_file()
        return ok, "" if ok else "Qwen-Image-2.1 weights not downloaded (run Initialize.bat / linux/initialize.sh in the tool folder)"

    def argv(self, tool_dir: Path, port: int, cfg: dict[str, Any]) -> list[str]:
        return [str(self.python(tool_dir)), "-m", "server", "--host", "127.0.0.1", "--port", str(port),
                "--no-browser", "--no-autoload"]

    def env(self, tool_dir: Path, cfg: dict[str, Any]) -> dict[str, str]:
        cache = tool_dir / ".cache"
        (cache / "tmp").mkdir(parents=True, exist_ok=True)
        return {**BASE_ENV, "HF_HOME": str(cache / "huggingface"), "HF_HUB_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1", "TRANSFORMERS_VERBOSITY": "error",
                "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True", "TMP": str(cache / "tmp"),
                "TEMP": str(cache / "tmp"), "QIS_RESTARTED": "1"}

    def pre_launch(self, tool_dir: Path, cfg: dict[str, Any]) -> list[str]:
        """Repair a stale absolute ``model_path`` after the tool folder was moved.

        Qwen Image Studio stores the absolute path of its weights in settings.json. When the whole
        folder is moved (e.g. into this hub's folder) that path no longer exists and the tool reports
        "Model weights not found" although the weights are right there. Point it back at the copy
        inside the tool folder when, and only when, the stored path is gone and the local one exists.
        """
        notes = super().pre_launch(tool_dir, cfg)
        settings_file = tool_dir / "settings.json"
        local = tool_dir / "models" / "Qwen-Image-2.1"
        if not settings_file.is_file() or not (local / "model_index.json").is_file():
            return notes
        try:
            data = json.loads(settings_file.read_text(encoding="utf-8"))
        except Exception:
            return notes
        stored = str(data.get("model_path") or "")
        if stored and not Path(stored).exists() and Path(stored).name == local.name:
            data["model_path"] = str(local)
            settings_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            notes.append(f"settings.json pointed at the moved folder {stored}; model_path now {local}")
        return notes

    def health_ok(self, status_code: int, body: Any) -> bool:
        return status_code == 200 and isinstance(body, dict) and "engine" in body

    def parse_status(self, body: dict[str, Any], extra: dict[str, Any]) -> Summary:
        eng = body.get("engine") or {}
        st = eng.get("status") or "unknown"
        queue = body.get("queue") or {}
        active = [j for j in (queue.get("active") or []) if isinstance(j, dict)]
        running = [j for j in active if j.get("status") == "running"]
        busy = bool(active)
        labels = {"unloaded": "Model not loaded", "loading": "Loading model…", "ready": "Model ready",
                  "error": "Model error"}
        label = labels.get(st, st.title())
        if st == "ready" and eng.get("profile_label"):
            label += f" · {eng['profile_label']}"
        job = None
        if running:
            j = running[0]
            params = j.get("params") or {}
            prog = j.get("progress") or {}
            kind = j.get("kind", "generate")
            title = (params.get("prompt") or "").strip()[:90] or {"enhance": "Enhancing prompt",
                                                                    "load_model": "Loading model"}.get(kind, kind)
            job = {"kind": kind, "title": title, "percent": _pct(prog.get("percent")), "eta": prog.get("eta"),
                   "message": prog.get("message") or "", "queued": max(0, len(active) - 1)}
        elif active:
            job = {"kind": active[0].get("kind"), "title": "Queued", "percent": None, "eta": None,
                   "message": f"{len(active)} job(s) queued", "queued": len(active)}
        gpu = body.get("gpu") or {}
        used = gpu.get("used_gb")
        return Summary(state="busy" if busy else st, loaded=st in ("loading", "ready"), busy=busy, label=label,
                       detail=(eng.get("message") or "") if st in ("loading", "error") else "",
                       model="Qwen-Image-2.1", job=job,
                       gpu_used_mb=int(float(used) * 1024) if used is not None else None)

    def unload_steps(self, base: str, cfg: dict[str, Any], tool_dir: Path) -> list[Step]:
        return [Step("POST", f"{base}/api/model/unload", timeout=90, label="unload Qwen-Image")]

    def prepare_steps(self, base: str, cfg: dict[str, Any], tool_dir: Path) -> list[Step]:
        return [Step("POST", f"{base}/api/model/load", json={}, timeout=15, label="load Qwen-Image")]

    def pre_forward_steps(self, base: str, method: str, path: str, summary: Summary) -> list[Step]:
        # The tool's job queue is sequential: a load job queued now runs before the generate job.
        if method == "POST" and path.startswith("/api/generate") and summary.state in ("unloaded", "error", "unknown", "off"):
            return [Step("POST", f"{base}/api/model/load", json={}, timeout=15, label="load Qwen-Image")]
        return []

    def vram_need_gb(self, total_gb: float) -> float:
        if total_gb >= 40:
            return 34.0
        if total_gb >= 22:
            return 20.0
        if total_gb >= 15:
            return 12.5
        return min(5.8, max(total_gb - 1.5, 3.0))   # the Minimal profile peaks at ~5.5 GB on an 8 GB card


# ======================================================================= 02 Voice Studio
class TtsTool(ToolSpec):
    id = "tts"
    name = "Voice Studio"
    short = "Voice"
    tagline = "Qwen3-TTS · preset voices, voice design, cloning, dialogue, dubbing"
    number = "02"
    color = "#6FCF97"
    icon = "mic"
    health_path = "/health"
    status_path = "/api/status"
    startup_timeout = 180.0
    port_regex = r"this PC\s*:\s*http://127\.0\.0\.1:(\d+)"
    outputs_rel = "app/outputs"
    editable_sources = {"qwen_tts": "Qwen3-TTS"}
    idle_context_mb = 800           # its own CUDA context plus the Chatterbox helper's
    checkout_markers = ("app/server.py",)
    docker_service = "qwen-tts"
    docker_port = 7861
    docker_extra_services = ("chatterbox",)
    model_delegate = True
    model_note = "Voice Studio downloads its models from HuggingFace into its own cache when they are loaded."

    import_slots = (
        {'id': 'clone', 'label': 'Voice clone - as the reference voice', 'kinds': ['audio']},
        {'id': 'sts', 'label': 'Speech to speech - as the source', 'kinds': ['audio']},
        {'id': 'dub', 'label': 'Dubbing - as the video (or audio)', 'kinds': ['video', 'audio']},
    )
    claim_routes = tuple((POST, p) for p in (
        "/api/tts/", "/api/models/load", "/api/tokenizer/", "/api/voices", "/api/sts", "/api/dub", "/api/transcribe",
        "/api/regenerate/", "/api/quality/", "/api/batch/csv", "/api/previews/generate", "/api/finetune/start",
        "/api/finetune/datasets", "/api/pronunciation/preview", "/v1/text-to-speech/", "/v1/speech-to-text",
    )) + ((GET_POST, "/api/subtitles/"),)

    def argv(self, tool_dir: Path, port: int, cfg: dict[str, Any]) -> list[str]:
        return [str(self.python(tool_dir)), "app/server.py", "--host", "127.0.0.1", "--port", str(port)]

    def helpers(self, tool_dir: Path, cfg: dict[str, Any]) -> list[Helper]:
        if cfg.get("chatterbox") is False:
            return []
        cb = tool_dir / ".venv-chatterbox" / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
        if not cb.is_file():
            return []
        return [Helper("chatterbox", [str(cb), "chatterbox_service/server.py", "--port", str(cfg.get("helper_port", 7862))],
                       tool_dir, dict(BASE_ENV))]

    def docker_services(self, tool_dir: Path, cfg: dict[str, Any]) -> tuple[list[str], tuple[str, ...]]:
        # Chatterbox is a second container (incompatible dependency set) sharing this one's network
        # namespace; it exists only when its image was built (run.sh --with-chatterbox / initialize.sh).
        tag = "rocm" if self.docker_backend(tool_dir) == "rocm" else "latest"
        if cfg.get("chatterbox", True) and dk.image_exists(f"ai/chatterbox:{tag}"):
            return [self.docker_service, "chatterbox"], ("chatterbox",)
        return [self.docker_service], ()

    def health_ok(self, status_code: int, body: Any) -> bool:
        return status_code == 200 and isinstance(body, dict) and body.get("app") == "tts-generator"

    def status_requests(self, base: str, body: dict[str, Any]) -> list[tuple[str, str]]:
        return [("queue", f"{base}/api/queue")]

    def parse_status(self, body: dict[str, Any], extra: dict[str, Any]) -> Summary:
        st = body.get("state") or "unknown"           # idle | loading | ready | error
        model = body.get("loaded_model") or body.get("loading_model") or ""
        aux = body.get("aux") or {}
        aux_bits = [n for k, n in (("whisper_loaded", "Whisper"), ("translator_loaded", "NLLB")) if aux.get(k)]
        cb = body.get("chatterbox") or {}
        if cb.get("loaded"):
            aux_bits.append("Chatterbox")
        jobs = [j for j in ((extra.get("queue") or {}).get("jobs") or []) if isinstance(j, dict)]
        busy = bool(jobs) or bool(body.get("finetune_running"))
        loaded = st in ("loading", "ready") or bool(aux_bits)
        if st == "ready":
            label = f"Model ready · {_short_model(model)}"
        elif st == "loading":
            label = f"Loading {_short_model(model)}…"
        elif st == "error":
            label = "Model error"
        else:
            label = "No model loaded"
        detail = (" + ".join(aux_bits) + " loaded") if aux_bits else (body.get("state_msg") or "")
        if st == "error":
            detail = body.get("state_msg") or detail
        job = None
        if jobs:
            j = jobs[0]
            kinds = {"custom_voice": "Preset voice", "voice_design": "Voice design", "voice_clone": "Voice clone",
                     "longform": "Long-form", "dialogue": "Dialogue", "sts": "Speech to speech", "dub": "Dubbing",
                     "load": "Loading model", "load_tokenizer": "Loading tokenizer", "transcribe": "Transcribing",
                     "clone_prompt": "Creating voice", "finetune": "Fine-tuning"}
            kind = j.get("kind") or "job"
            job = {"kind": kind, "title": kinds.get(kind, kind.replace("_", " ").title()), "percent": None,
                   "eta": None, "message": j.get("message") or ("Running" if j.get("status") == "running" else "Queued"),
                   "queued": max(0, len(jobs) - 1)}
        if body.get("finetune_running") and not job:
            job = {"kind": "finetune", "title": "Fine-tuning", "percent": None, "eta": None, "message": "Training", "queued": 0}
        gpu = body.get("gpu") or {}
        return Summary(state="busy" if busy else ("ready" if st == "ready" else ("loading" if st == "loading" else
                                                                            ("error" if st == "error" else "unloaded"))),
                       loaded=loaded, busy=busy, label=label, detail=detail, model=_short_model(model), job=job,
                       gpu_used_mb=gpu.get("used_mb"))

    def unload_steps(self, base: str, cfg: dict[str, Any], tool_dir: Path) -> list[Step]:
        steps = [Step("POST", f"{base}/api/models/unload", timeout=60, label="unload Qwen3-TTS"),
                 Step("POST", f"{base}/api/tokenizer/unload", timeout=30, label="unload tokenizer"),
                 Step("POST", f"{base}/api/aux/unload", timeout=30, label="unload Whisper/NLLB")]
        helper_port = cfg.get("helper_port")
        if helper_port:
            steps.append(Step("POST", f"http://127.0.0.1:{helper_port}/unload", timeout=30, label="unload Chatterbox"))
        return steps

    def last_model(self, tool_dir: Path) -> str:
        """The model of the most recent clip, so warming up loads what the user actually uses."""
        try:
            hist = json.loads((tool_dir / "app" / "outputs" / "history.json").read_text(encoding="utf-8"))
            for entry in hist:
                m = entry.get("model")
                if isinstance(m, str) and m.startswith("Qwen/"):
                    return m
        except Exception:
            pass
        return DEFAULT_TTS_MODEL

    def prepare_steps(self, base: str, cfg: dict[str, Any], tool_dir: Path) -> list[Step]:
        return [Step("POST", f"{base}/api/models/load", json={"model_id": self.last_model(tool_dir)}, timeout=15,
                     label="load Qwen3-TTS")]

    def vram_need_gb(self, total_gb: float) -> float:
        return 5.2


# ======================================================================= 03 Video Studio
class VideoTool(ToolSpec):
    id = "video"
    name = "Video Studio"
    short = "Video"
    tagline = "LTX-2.5 & MiniMax H3 · text, image and keyframes to video with audio"
    number = "03"
    color = "#F28B82"
    icon = "video"
    health_path = "/api/system"
    status_path = "/api/system"
    startup_timeout = 180.0
    port_regex = r"Lumen Video Studio -> http://[\w.\-]+:(\d+)"
    claim_routes = ((POST, "/api/generate"), (POST, "/api/engine/start"), (POST, "/api/engine/restart"))
    idle_context_mb = 0             # the backend never touches CUDA; only its (separate) engine process does
    checkout_markers = ("backend/run.py",)
    docker_service = "video"
    docker_port = 8765
    model_delegate = True
    model_note = "Video Studio has its own model packs with resumable downloads; the hub shows and drives them."

    import_slots = (
        {'id': 'i2v', 'label': 'Image to video - as the start frame', 'kinds': ['image']},
        {'id': 'flf_start', 'label': 'First + last frame - as the first frame', 'kinds': ['image']},
        {'id': 'flf_end', 'label': 'First + last frame - as the last frame', 'kinds': ['image']},
        {'id': 'swap_video', 'label': 'Character swap - as the video to change', 'kinds': ['video']},
        {'id': 'swap_character', 'label': 'Character swap - as the new character', 'kinds': ['image']},
    )

    def models_dir(self, tool_dir: Path) -> Path:
        """The repo's models/ - or, on Linux, the ComfyUI model store linux/.env points at."""
        env = dk.read_env_file(tool_dir / "linux" / ".env") if IS_LINUX else {}
        custom = env.get("VIDEOGEN_MODELS_DIR")
        if custom:
            p = Path(custom)
            if not p.is_absolute():
                p = (tool_dir / "linux" / p).resolve()
            return p
        return tool_dir / "models"

    def model_present(self, tool_dir: Path) -> tuple[bool, str]:
        d = self.models_dir(tool_dir) / "diffusion_models"
        ok = d.is_dir() and any(p.is_file() and not p.name.endswith(".part") and not p.name.startswith("put_")
                                for p in d.iterdir())
        return ok, "" if ok else "No video model pack downloaded yet (open Video Studio → Models)"

    def argv(self, tool_dir: Path, port: int, cfg: dict[str, Any]) -> list[str]:
        return [str(self.python(tool_dir)), "backend/run.py", "--host", "127.0.0.1", "--port", str(port),
                "--no-port-fallback"]

    def pre_launch(self, tool_dir: Path, cfg: dict[str, Any]) -> list[str]:
        """Keep Lumen from starting its ComfyUI engine (and taking the GPU) the moment it boots.

        The engine still starts automatically for every generation; the hub merely decides *when*.
        """
        notes = super().pre_launch(tool_dir, cfg)
        settings_file = tool_dir / "data" / "settings.json"
        try:
            data = json.loads(settings_file.read_text(encoding="utf-8")) if settings_file.is_file() else {}
        except Exception:
            data = {}
        if data.get("engine_autostart") is False:
            return notes
        data["engine_autostart"] = False
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        notes.append("set engine_autostart=false in data/settings.json so the engine starts only when needed")
        return notes

    def health_ok(self, status_code: int, body: Any) -> bool:
        return status_code == 200 and isinstance(body, dict) and "engine" in body

    def status_requests(self, base: str, body: dict[str, Any]) -> list[tuple[str, str]]:
        cur = (body.get("queue") or {}).get("current_local")
        return [("job", f"{base}/api/jobs/{cur}")] if cur else []

    @staticmethod
    def _active_downloads(downloads: Any) -> int:
        items = downloads.values() if isinstance(downloads, dict) else downloads if isinstance(downloads, list) else []
        n = 0
        for it in items:
            if isinstance(it, dict) and str(it.get("status", "")).lower() in ("downloading", "queued", "running", "active"):
                n += 1
        return n

    def parse_status(self, body: dict[str, Any], extra: dict[str, Any]) -> Summary:
        eng = body.get("engine") or {}
        st = eng.get("state") or "unknown"           # stopped | starting | running | error
        queue = body.get("queue") or {}
        cur = queue.get("current_local")
        pending = int(queue.get("pending_local") or 0)
        busy = bool(cur) or pending > 0
        loaded = st in ("starting", "running")
        labels = {"running": "Engine online", "starting": "Engine starting…", "stopped": "Engine offline",
                  "error": "Engine error"}
        label = labels.get(st, st.title())
        detail = eng.get("error") or ""
        dl = self._active_downloads(body.get("downloads"))
        if dl and not detail:
            detail = f"{dl} model download(s) in progress"
        job = None
        j = extra.get("job")
        if isinstance(j, dict) and j.get("id"):
            prog = j.get("progress")
            job = {"kind": j.get("mode") or "generate", "title": (j.get("prompt") or "").strip()[:90] or "Rendering video",
                   "percent": _pct(float(prog) * 100) if isinstance(prog, (int, float)) else None, "eta": None,
                   "message": j.get("stage") or j.get("status") or "", "queued": pending}
        elif busy:
            job = {"kind": "generate", "title": "Queued", "percent": None, "eta": None, "message": f"{pending} queued",
                   "queued": pending}
        gpu = (body.get("system") or {}).get("gpu") or {}
        return Summary(state="busy" if busy else ("ready" if st == "running" else ("loading" if st == "starting" else
                                                                                 ("error" if st == "error" else "unloaded"))),
                       loaded=loaded, busy=busy, label=label, detail=detail, model="LTX-2.5 / MiniMax H3", job=job,
                       gpu_used_mb=gpu.get("vram_used_mb"))

    def unload_steps(self, base: str, cfg: dict[str, Any], tool_dir: Path) -> list[Step]:
        return [Step("POST", f"{base}/api/engine/unload", timeout=60, label="free ComfyUI models"),
                Step("POST", f"{base}/api/engine/stop", timeout=60, label="stop ComfyUI engine")]

    def prepare_steps(self, base: str, cfg: dict[str, Any], tool_dir: Path) -> list[Step]:
        return [Step("POST", f"{base}/api/engine/start", timeout=3, label="start ComfyUI engine", fire_and_forget=True)]

    def vram_need_gb(self, total_gb: float) -> float:
        if total_gb >= 24:
            return 20.0
        if total_gb >= 16:
            return 13.0
        if total_gb >= 12:
            return 10.0
        return max(total_gb - 1.6, 5.0)           # the engine streams the rest from RAM anyway


# ======================================================================= 04 Music Studio
class MusicTool(ToolSpec):
    id = "music"
    name = "Music Studio"
    short = "Music"
    tagline = "YuE2 · lyrics and a style prompt to a full song with an editable score"
    number = "04"
    color = "#A78BFA"
    icon = "music"
    health_path = "/api/status"
    status_path = "/api/status"
    startup_timeout = 240.0
    port_regex = r"Music Gen Studio -> http://[\w.\-]+:(\d+)"
    python_rel = "YuE/.venv/Scripts/python.exe" if sys.platform == "win32" else "YuE/.venv/bin/python"
    editable_sources = {"yue2_infer": "YuE"}
    checkout_markers = ("webui.py",)
    docker_service = "yue2"
    docker_port = 7863
    supports_unload = False
    model_kinds = (
        {"id": "model", "label": "Models", "dir": "models", "layout": "folder", "use": True, "sources": ["hf"], "catalog": [
            {"name": "YuE2-3B", "label": "YuE2-3B", "detail": "The song model (the only one the studio loads)", "size_h": "~6.8 GB",
             "source": {"type": "hf", "repo": "m-a-p/YuE2-3B", "allow_patterns": ["*.json", "*.txt", "*.py", "*.safetensors", "*.model", "*.tiktoken", "licenses/*"]}},
            {"name": "YuE2-Vae", "label": "YuE2 VAE", "detail": "Audio decoder", "size_h": "~0.5 GB",
             "source": {"type": "hf", "repo": "m-a-p/YuE2-Vae", "allow_patterns": ["*.json", "*.txt", "*.py", "*.safetensors", "licenses/*"]}},
            {"name": "YuE2-Vae-legacy", "label": "YuE2 VAE (legacy)", "detail": "The decoder used for the published benchmarks", "size_h": "~0.5 GB",
             "source": {"type": "hf", "repo": "m-a-p/YuE2-Vae-legacy", "allow_patterns": ["*.json", "*.txt", "*.py", "*.safetensors", "licenses/*"]}},
            {"name": "SheetSage2", "label": "SheetSage2", "detail": "Transcribes a recording for the cover feature", "size_h": "~0.2 GB",
             "source": {"type": "hf", "repo": "m-a-p/SheetSage2", "allow_patterns": ["*.py", "*.json", "*.txt", "*.safetensors"]}},
            {"name": "MERT-v2-FullSong", "label": "MERT-v2-FullSong", "detail": "Audio encoder for SheetSage2", "size_h": "~2.4 GB",
             "source": {"type": "hf", "repo": "m-a-p/MERT-v2-FullSong", "allow_patterns": ["*.py", "*.json", "*.safetensors"]}}]},
    )
    model_note = "The song model is any YuE2 stage-1 folder in models/ (Use switches it); the VAEs, SheetSage2 and MERT keep their names."

    import_slots = (
        {'id': 'voice', 'label': 'Sing it in this voice - as the voice reference', 'kinds': ['audio']},
        {'id': 'cover', 'label': 'Cover this recording', 'kinds': ['audio']},
    )
    claim_routes = ((POST, "/api/generate"), (POST, "/api/plan"), (POST, "/api/transcribe"), (POST, "/api/lyrics"),
                    (POST, "/api/describe"), (POST, "/api/songs/"))
    claim_exclude_suffixes = ("/export", "/share", "/meta")

    def model_present(self, tool_dir: Path) -> tuple[bool, str]:
        ok = (tool_dir / "models" / "YuE2-3B").is_dir() and (tool_dir / "models" / "YuE2-Vae").is_dir()
        return ok, "" if ok else "YuE2 weights not downloaded (run initialize.bat / linux/initialize.sh in the tool folder)"

    def argv(self, tool_dir: Path, port: int, cfg: dict[str, Any]) -> list[str]:
        return [str(self.python(tool_dir)), "webui.py", "--host", "127.0.0.1", "--port", str(port)]

    def health_ok(self, status_code: int, body: Any) -> bool:
        return status_code == 200 and isinstance(body, dict) and "model" in body

    def parse_status(self, body: dict[str, Any], extra: dict[str, Any]) -> Summary:
        m = body.get("model") or {}
        st = m.get("state") or "unknown"                # loading | ready | error
        cur = body.get("current")
        queue = body.get("queue") or []
        busy = cur is not None or bool(queue)
        mode = m.get("vram_mode") or ""
        if st == "loading":
            label, detail = "Loading model into RAM…", "The GPU is only used while a song renders"
        elif st == "ready":
            label, detail = f"Ready in RAM · {mode} VRAM mode" if mode else "Ready in RAM", ""
        elif st == "error":
            label, detail = "Model error", m.get("error") or ""
        else:
            label, detail = st.title(), ""
        job = None
        if isinstance(cur, dict):
            stage = cur.get("stage")
            message, percent = "", None
            if isinstance(stage, dict):
                message = str(stage.get("label") or stage.get("name") or stage.get("status") or "")
                done, total = stage.get("done"), stage.get("total")
                if isinstance(done, (int, float)) and isinstance(total, (int, float)) and total:
                    percent = _pct(done / total * 100)
            elif isinstance(stage, str):
                message = stage
            job = {"kind": cur.get("kind") or "song", "title": (cur.get("title") or "Song")[:90], "percent": percent,
                   "eta": None, "message": message, "queued": len(queue)}
        elif queue:
            job = {"kind": "song", "title": "Queued", "percent": None, "eta": None, "message": f"{len(queue)} queued",
                   "queued": len(queue)}
        gpu = body.get("gpu") or {}
        used = gpu.get("used_gib")
        return Summary(state="busy" if busy else ("ready" if st == "ready" else st), loaded=busy, busy=busy, label=label,
                       detail=detail, model="YuE2-3B", job=job,
                       gpu_used_mb=int(float(used) * 1024) if used is not None else None)

    def vram_need_gb(self, total_gb: float) -> float:
        return 8.2 if total_gb >= 14 else 5.6



# ======================================================================= 05 Forge Studio
class ForgeTool(ToolSpec):
    """Stable Diffusion WebUI Forge (Neo) - the "Jrlabanza Image Generator" build.

    Forge is a Gradio app; besides its REST API (``/sdapi/v1/...``, enabled with ``--api``) every
    action in its UI travels through Gradio's queue (``/queue/join``), so that route is a claim too.
    """
    id = "forge"
    name = "Forge Studio"
    short = "Forge"
    tagline = "Forge Neo · Stable Diffusion checkpoints, LoRAs, ControlNet, upscaling"
    number = "05"
    color = "#E05A4E"
    icon = "forge"
    health_path = "/internal/ping"
    status_path = "/sdapi/v1/progress?skip_current_image=true"
    startup_timeout = 300.0
    port_regex = r"Running on local URL:\s+http://[\w.\-]+:(\d+)"
    outputs_rel = "output"
    python_rel = "venv/Scripts/python.exe" if sys.platform == "win32" else "venv/bin/python"
    claim_routes = tuple((POST, p) for p in (
        "/sdapi/v1/txt2img", "/sdapi/v1/img2img", "/sdapi/v1/extra-single-image", "/sdapi/v1/extra-batch-images",
        "/sdapi/v1/reload-checkpoint", "/queue/join", "/gradio_api/queue/join", "/run/predict", "/api/predict",
    ))
    checkout_markers = ("launch.py", "modules/api/api.py")
    docker_service = "forge"
    docker_port = 7860
    idle_context_mb = 500
    model_kinds = (
        {"id": "checkpoint", "label": "Checkpoints", "dir": "models/Stable-diffusion", "layout": "file", "exts": [".safetensors", ".ckpt", ".gguf"],
         "use": True, "sources": ["civitai", "url", "hf_file"], "civitai": {"types": "Checkpoint", "base": ""}},
        {"id": "lora", "label": "LoRAs", "dir": "models/Lora", "layout": "file", "exts": [".safetensors", ".pt"], "recursive": True,
         "sources": ["civitai", "url", "hf_file"], "civitai": {"types": "LORA", "base": ""}},
        {"id": "vae", "label": "VAEs", "dir": "models/VAE", "layout": "file", "exts": [".safetensors", ".pt", ".ckpt"],
         "sources": ["civitai", "url", "hf_file"], "civitai": {"types": "VAE", "base": ""}},
        {"id": "controlnet", "label": "ControlNet", "dir": "models/ControlNet", "layout": "file", "exts": [".safetensors", ".pth", ".bin"],
         "sources": ["civitai", "url", "hf_file"], "civitai": {"types": "Controlnet", "base": ""}},
        {"id": "upscaler", "label": "Upscalers", "dir": "models/ESRGAN", "layout": "file", "exts": [".pth", ".safetensors"],
         "sources": ["url", "hf_file"]},
    )
    model_note = "Checkpoints, LoRAs, VAEs and ControlNets go in Forge's models folders; Civitai search is built in."

    import_slots = (
        {'id': 'edit', 'label': 'Studio - Edit this image', 'kinds': ['image']},
        {'id': 'img2img', 'label': 'Classic - img2img source', 'kinds': ['image']},
    )

    def model_present(self, tool_dir: Path) -> tuple[bool, str]:
        d = tool_dir / "models" / "Stable-diffusion"
        ok = d.is_dir() and any(p.suffix in (".safetensors", ".ckpt", ".gguf") for p in d.iterdir())
        return ok, "" if ok else "No checkpoint in models/Stable-diffusion yet"

    def argv(self, tool_dir: Path, port: int, cfg: dict[str, Any]) -> list[str]:
        # The flags of webui-user.bat, tuned for an 8 GB card; see the Linux entrypoint for the reasoning.
        argv = [str(self.python(tool_dir)), "launch.py", "--port", str(port), "--api", "--reserve-vram", "2",
                "--skip-python-version-check", "--skip-version-check", "--skip-torch-cuda-test", "--disable-gpu-warning"]
        if self.gpu_profile(tool_dir).get("backend", "cuda") == "cuda":
            argv += ["--pin-shared-memory", "--cuda-malloc", "--cuda-stream"]      # CUDA allocator / stream tricks
        else:
            argv += ["--use-pytorch-cross-attention"]                              # ROCm: SDPA, no SageAttention / xformers
        if (tool_dir / "tools" / ".portable").is_file():
            argv.append("--skip-install")
        return argv

    def health_ok(self, status_code: int, body: Any) -> bool:
        return status_code == 200

    def status_requests(self, base: str, body: dict[str, Any]) -> list[tuple[str, str]]:
        return [("memory", f"{base}/sdapi/v1/memory")]

    def parse_status(self, body: dict[str, Any], extra: dict[str, Any]) -> Summary:
        st = body.get("state") or {}
        job_count = int(st.get("job_count") or 0)
        progress = float(body.get("progress") or 0.0)
        busy = job_count > 0 or progress > 0
        cuda = ((extra.get("memory") or {}).get("cuda") or {})
        allocated = ((cuda.get("allocated") or {}).get("current") or 0) if isinstance(cuda, dict) else 0
        reserved = ((cuda.get("reserved") or {}).get("current") or 0) if isinstance(cuda, dict) else 0
        loaded = max(allocated, reserved) > 200 * 1024 * 1024
        job = None
        if busy:
            steps, step = int(st.get("sampling_steps") or 0), int(st.get("sampling_step") or 0)
            job = {"kind": "generate", "title": (st.get("job") or "Generating").strip()[:90] or "Generating",
                   "percent": _pct(progress * 100), "eta": body.get("eta_relative") or None,
                   "message": f"step {step}/{steps}" if steps else (body.get("textinfo") or ""), "queued": max(0, job_count - 1)}
        label = "Checkpoint loaded" if loaded else "No checkpoint loaded"
        return Summary(state="busy" if busy else ("ready" if loaded else "unloaded"), loaded=loaded, busy=busy,
                       label=label, detail="", model="Forge Neo", job=job,
                       gpu_used_mb=int(reserved / 1024 / 1024) if reserved else None)

    def unload_steps(self, base: str, cfg: dict[str, Any], tool_dir: Path) -> list[Step]:
        return [Step("POST", f"{base}/sdapi/v1/unload-checkpoint", timeout=90, label="unload Forge checkpoint")]

    def vram_need_gb(self, total_gb: float) -> float:
        if total_gb >= 20:
            return 10.0
        if total_gb >= 12:
            return 8.0
        return min(6.0, max(total_gb - 1.5, 3.0))   # --reserve-vram 2 keeps the rest free


TOOLS: dict[str, ToolSpec] = {t.id: t for t in (ImageTool(), TtsTool(), VideoTool(), MusicTool(), ForgeTool())}
TOOL_ORDER = ["image", "tts", "video", "music", "forge"]
