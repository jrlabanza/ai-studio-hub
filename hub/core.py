"""The Hub object wires settings, telemetry, the tool processes, the orchestrator and the library."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from . import __version__
from .config import APP_NAME, ROOT, ensure_dirs, load_settings
from .events import EventBus
from .gpu import GpuMonitor
from .library import Library
from .orchestrator import Orchestrator
from .process import ManagedProcess
from .tools import TOOL_ORDER, TOOLS


class Hub:
    def __init__(self) -> None:
        ensure_dirs()
        self.bus = EventBus()
        self.gpu = GpuMonitor(interval=2.0)
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(10.0), trust_env=False)
        self.tools: dict[str, ManagedProcess] = {tid: ManagedProcess(TOOLS[tid], self) for tid in TOOL_ORDER}
        self.orchestrator = Orchestrator(self)
        self.library = Library(self)
        self.started_at = time.time()
        self.hub_port: int = load_settings().hub_port
        self.proxy_ports: dict[str, int] = {}
        self._bg: list[asyncio.Task] = []

    @property
    def settings(self):
        return load_settings()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.bus.loop = asyncio.get_running_loop()
        await self.gpu.start()
        await self.orchestrator.start()
        g = self.gpu.latest
        if g.available:
            self.bus.log(f"{APP_NAME} {__version__} ready · {g.name} · {g.total_mb / 1024:.0f} GB VRAM · "
                         f"policy: {self.orchestrator.policy()}", source="hub")
        else:
            self.bus.log(f"{APP_NAME} {__version__} ready · no NVIDIA GPU detected", level="warn", source="hub")
        for tool in self.tools.values():
            if tool.enabled and tool.cfg.get("autostart"):
                self._bg.append(asyncio.create_task(tool.start()))

    async def stop(self) -> None:
        await self.orchestrator.stop()
        await self.gpu.stop()
        if self.settings.stop_tools_on_exit:
            await asyncio.gather(*(t.stop("hub shutting down") for t in self.tools.values() if t.state != "stopped"),
                                 return_exceptions=True)
        await self.http.aclose()

    # ------------------------------------------------------------------ state
    def snapshot(self) -> dict[str, Any]:
        s = self.settings
        tools = {}
        for tid, t in self.tools.items():
            d = t.describe()
            d["summary"] = self.orchestrator.summary(tid).to_dict()
            d["vram_need_gb"] = round(t.spec.vram_need_gb(self.gpu.latest.total_mb / 1024 or 8), 1)
            d["last_activity"] = self.orchestrator.last_activity.get(tid)
            d["is_owner"] = self.orchestrator.owner == tid
            d["proxy_port"] = self.proxy_ports.get(tid, d.get("proxy_port"))
            tools[tid] = d
        return {
            "app": {"name": APP_NAME, "version": __version__, "hub_port": self.hub_port, "root": str(ROOT),
                    "uptime_s": round(time.time() - self.started_at), "bind_host": s.bind_host},
            "system": self.gpu.snapshot,
            "gpu_history": [[round(ts), used, util] for ts, used, util in self.gpu.history],
            "tools": tools,
            "order": TOOL_ORDER,
            "orchestrator": self.orchestrator.describe(),
            "theme": s.theme,
            "settings": s.to_dict(),
            "clients": self.bus.connected,
        }
