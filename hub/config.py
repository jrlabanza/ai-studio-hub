"""Paths and persisted settings of the hub.

Everything the hub writes lives in ``data/`` next to this package. The tools themselves are never
modified, with one documented exception: Lumen's ``engine_autostart`` flag (see tools.py).
"""
from __future__ import annotations

import copy
import json
import sys
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

HUB_DIR = Path(__file__).resolve().parent
ROOT = HUB_DIR.parent
DATA_DIR = ROOT / "data"
LOGS_DIR = DATA_DIR / "logs"
THUMBS_DIR = DATA_DIR / "thumbs"
WEB_DIR = HUB_DIR / "web"
BRAND_DIR = WEB_DIR / "brand"
SETTINGS_FILE = DATA_DIR / "settings.json"
PID_FILE = DATA_DIR / "hub.pid"

APP_NAME = "AI Studio Hub"
IS_WINDOWS = sys.platform == "win32"
PLATFORM = "windows" if IS_WINDOWS else ("linux" if sys.platform.startswith("linux") else sys.platform)

# Per-tool defaults. ``dir`` is the folder name inside the hub (the git-submodule layout) or next to
# it; ``aliases`` are the other names the same repository is commonly checked out under (its GitHub
# name, the name the Linux ``~/AI`` layout uses) and are searched too. ``port`` is the tool's own
# backend port (loopback only); ``proxy_port`` is the branded, orchestrated entrance the shell embeds.
# On Linux the studios run in containers whose ports are fixed by their compose files, so the
# defaults differ per platform (7860 belongs to Forge there, not to Music Studio).
DEFAULT_TOOLS: dict[str, dict[str, Any]] = {
    "image": {"enabled": True, "dir": "Image gen", "aliases": ["qwen-image-studio", "image-gen", "Image Studio"],
              "port": 7970 if IS_WINDOWS else 7864, "proxy_port": 7901, "autostart": False, "pinned": False},
    "tts": {"enabled": True, "dir": "qwen tts", "aliases": ["tts-generator", "qwen-tts", "Voice Studio"],
            "port": 7861, "helper_port": 7862, "proxy_port": 7902, "autostart": False, "pinned": False,
            "chatterbox": True},
    "video": {"enabled": True, "dir": "video-gen", "aliases": ["video-generator", "lumen", "Video Studio"],
              "port": 8765, "proxy_port": 7903, "autostart": False, "pinned": False},
    "music": {"enabled": True, "dir": "Yue2", "aliases": ["music-generator", "yue2", "Music Studio"],
              "port": 7860 if IS_WINDOWS else 7863, "proxy_port": 7904, "autostart": False, "pinned": False},
    # Optional studio: appears only when a checkout is found (a clone without access to its private
    # repository still runs the four above).
    "forge": {"enabled": True, "dir": "forge", "aliases": ["jrlabanza-image-generator-core", "Forge", "Forge Neo",
                                                          "stable-diffusion-webui-forge"],
              "port": 7866 if IS_WINDOWS else 7860, "proxy_port": 7905, "autostart": False, "pinned": False,
              "optional": True},
}
TOOL_KEYS = {"enabled", "dir", "port", "helper_port", "proxy_port", "autostart", "pinned", "chatterbox"}


def ensure_dirs() -> None:
    for d in (DATA_DIR, LOGS_DIR, THUMBS_DIR):
        d.mkdir(parents=True, exist_ok=True)


@dataclass
class Settings:
    theme: str = "light"                  # light | dark
    hub_port: int = 7900
    bind_host: str = "127.0.0.1"          # 0.0.0.0 to share on the LAN (see README)
    open_browser: bool = True
    gpu_policy: str = "auto"              # auto | exclusive | budget
    prepare_on_switch: bool = True        # load the model of the tool you switch to, ahead of time
    prepare_delay_s: float = 3.0
    idle_unload_min: float = 0.0          # 0 = automatic (depends on VRAM size), -1 = never
    idle_stop_min: float = 0.0            # 0 = automatic, -1 = never
    claim_wait_max_min: float = 45.0      # how long a generate request may wait for another tool's job
    vram_headroom_gb: float = 0.4
    release_ollama: bool = True           # ask a local Ollama to drop its models when VRAM is needed
    restyle_tools: bool = True            # apply the hub theme inside the embedded tools
    brand_fonts_in_tools: bool = True
    stop_tools_on_exit: bool = True
    library_page_size: int = 60
    tools: dict[str, dict[str, Any]] = field(default_factory=lambda: copy.deepcopy(DEFAULT_TOOLS))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_lock = threading.Lock()
_settings: Settings | None = None


def _merge_tools(base: dict[str, dict[str, Any]], patch: dict[str, Any]) -> None:
    for tool_id, values in (patch or {}).items():
        if tool_id not in DEFAULT_TOOLS or not isinstance(values, dict):
            continue
        cur = base.setdefault(tool_id, copy.deepcopy(DEFAULT_TOOLS[tool_id]))
        for k, v in values.items():
            if k in TOOL_KEYS:
                cur[k] = v


def load_settings() -> Settings:
    global _settings
    with _lock:
        if _settings is not None:
            return _settings
        s = Settings()
        if SETTINGS_FILE.exists():
            try:
                data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                for k, v in data.items():
                    if k == "tools":
                        _merge_tools(s.tools, v)
                    elif hasattr(s, k):
                        setattr(s, k, v)
            except Exception as exc:  # corrupt file: keep defaults
                print(f"[settings] could not read {SETTINGS_FILE}: {exc}")
        _settings = s
        return s


def save_settings(patch: dict[str, Any]) -> Settings:
    s = load_settings()
    with _lock:
        for k, v in patch.items():
            if k == "tools" and isinstance(v, dict):
                _merge_tools(s.tools, v)
            elif hasattr(s, k) and k != "tools":
                cur = getattr(s, k)
                try:
                    if isinstance(cur, bool):
                        v = bool(v)
                    elif isinstance(cur, int):
                        v = int(v)
                    elif isinstance(cur, float):
                        v = float(v)
                    else:
                        v = str(v)
                except (TypeError, ValueError):
                    continue
                setattr(s, k, v)
        ensure_dirs()
        SETTINGS_FILE.write_text(json.dumps(s.to_dict(), indent=2), encoding="utf-8")
    return s


def tool_cfg(tool_id: str) -> dict[str, Any]:
    return load_settings().tools.get(tool_id) or copy.deepcopy(DEFAULT_TOOLS[tool_id])


def tool_dir_candidates(tool_id: str) -> list[Path]:
    """Every folder the tool could live in, in order of preference."""
    cfg = tool_cfg(tool_id)
    p = Path(cfg.get("dir") or DEFAULT_TOOLS[tool_id]["dir"])
    if p.is_absolute():
        return [p]
    names: list[str] = [str(p)]
    for alias in [DEFAULT_TOOLS[tool_id]["dir"], *DEFAULT_TOOLS[tool_id].get("aliases", [])]:
        if alias not in names:
            names.append(alias)
    out: list[Path] = []
    for base in (ROOT, ROOT.parent):
        for name in names:
            cand = base / name
            if cand.is_dir() and cand not in out:
                out.append(cand)
    return out


def tool_present(tool_id: str) -> bool:
    """False for an optional studio that has no checkout anywhere the hub looks (it is then left out of the shell)."""
    if not DEFAULT_TOOLS[tool_id].get("optional"):
        return True
    from .tools import TOOLS

    return any(TOOLS[tool_id].is_checkout(c) for c in tool_dir_candidates(tool_id))


def tool_dir(tool_id: str) -> Path:
    """Where a tool lives: an absolute path from Settings, or a folder inside this hub's folder (the
    git-submodule layout), or one of the same/known folder names beside the hub's folder (the sibling
    layout, e.g. ``~/AI/<repo>`` on Linux).

    When several exist - e.g. an un-set-up submodule checkout inside plus an installed copy next to
    the hub - the copy that is actually set up (has its Python environment or its container image)
    wins; failing that, the first one that at least holds a checkout.
    """
    candidates = tool_dir_candidates(tool_id)
    if not candidates:
        return ROOT / (tool_cfg(tool_id).get("dir") or DEFAULT_TOOLS[tool_id]["dir"])
    if len(candidates) == 1:
        return candidates[0]
    from .tools import TOOLS  # local import: tools.py is independent of this module

    spec = TOOLS[tool_id]
    for cand in candidates:
        if spec.installed(cand)[0]:
            return cand
    for cand in candidates:
        if spec.is_checkout(cand):
            return cand
    return candidates[0]
