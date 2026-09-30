"""Paths and persisted settings of the hub.

Everything the hub writes lives in ``data/`` next to this package. The tools themselves are never
modified, with one documented exception: Lumen's ``engine_autostart`` flag (see tools.py).
"""
from __future__ import annotations

import copy
import json
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

APP_NAME = "AI Studio Hub"

# Per-tool defaults. ``dir`` is relative to ROOT unless absolute. ``port`` is the tool's own
# backend port (loopback only); ``proxy_port`` is the branded, orchestrated entrance the shell embeds.
DEFAULT_TOOLS: dict[str, dict[str, Any]] = {
    "image": {"enabled": True, "dir": "Image gen", "port": 7970, "proxy_port": 7901, "autostart": False,
              "pinned": False},
    "tts": {"enabled": True, "dir": "qwen tts", "port": 7861, "helper_port": 7862, "proxy_port": 7902,
            "autostart": False, "pinned": False},
    "video": {"enabled": True, "dir": "video-gen", "port": 8765, "proxy_port": 7903, "autostart": False,
              "pinned": False},
    "music": {"enabled": True, "dir": "Yue2", "port": 7860, "proxy_port": 7904, "autostart": False,
              "pinned": False},
}
TOOL_KEYS = {"enabled", "dir", "port", "helper_port", "proxy_port", "autostart", "pinned"}


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


def tool_dir(tool_id: str) -> Path:
    """Where a tool lives: an absolute path from Settings, or a folder inside this hub's folder, or
    (so the hub can be cloned *next to* the tools) the same folder name beside the hub's folder."""
    cfg = tool_cfg(tool_id)
    p = Path(cfg.get("dir") or DEFAULT_TOOLS[tool_id]["dir"])
    if p.is_absolute():
        return p
    for base in (ROOT, ROOT.parent):
        if (base / p).is_dir():
            return base / p
    return ROOT / p
