"""GPU / RAM telemetry without importing torch: nvidia-smi (about 30 ms per call) and psutil."""
from __future__ import annotations

import asyncio
import collections
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import psutil

from .config import ROOT

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _nvidia_smi_path() -> str | None:
    exe = shutil.which("nvidia-smi")
    if exe:
        return exe
    for cand in (r"C:\Windows\System32\nvidia-smi.exe",
                 r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
        if os.path.exists(cand):
            return cand
    return None


_SMI = _nvidia_smi_path()


def _smi(args: list[str], timeout: float = 4.0) -> str | None:
    if not _SMI:
        return None
    try:
        out = subprocess.run([_SMI, *args], capture_output=True, text=True, timeout=timeout,
                             creationflags=_CREATE_NO_WINDOW)
        if out.returncode != 0:
            return None
        return out.stdout.strip()
    except Exception:
        return None


def _num(v: str, default: float = 0.0) -> float:
    v = v.strip()
    if not v or v.startswith("[N/A]") or v == "N/A":
        return default
    try:
        return float(v)
    except ValueError:
        return default


@dataclass
class GpuInfo:
    available: bool
    name: str = ""
    total_mb: int = 0
    used_mb: int = 0
    free_mb: int = 0
    util: int = 0
    temp: int = 0
    power_w: float = 0.0
    driver: str = ""
    compute_cap: str = ""
    processes: list[dict[str, Any]] = field(default_factory=list)


def query_gpu(with_processes: bool = False) -> GpuInfo:
    line = _smi(["--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu,power.draw,"
                 "driver_version,compute_cap", "--format=csv,noheader,nounits"])
    if not line:
        return GpuInfo(available=False)
    parts = [p.strip() for p in line.splitlines()[0].split(",")]
    try:
        info = GpuInfo(available=True, name=parts[0], total_mb=int(_num(parts[1])), used_mb=int(_num(parts[2])),
                       free_mb=int(_num(parts[3])), util=int(_num(parts[4])), temp=int(_num(parts[5])),
                       power_w=round(_num(parts[6]), 1), driver=parts[7] if len(parts) > 7 else "",
                       compute_cap=parts[8] if len(parts) > 8 else "")
    except Exception:
        info = GpuInfo(available=True, name=parts[0] if parts else "GPU")
    if with_processes:
        procs = _smi(["--query-compute-apps=pid,used_memory,process_name", "--format=csv,noheader,nounits"], timeout=3)
        if procs:
            for row in procs.splitlines():
                cols = [c.strip() for c in row.split(",")]
                if len(cols) >= 2 and cols[0].isdigit():
                    used = _num(cols[1], -1)
                    info.processes.append({"pid": int(cols[0]), "used_mb": int(used) if used >= 0 else None,
                                           "name": cols[2] if len(cols) > 2 else ""})
    return info


def vram_tier(total_mb: int) -> str:
    gb = total_mb / 1024
    if gb <= 0:
        return "none"
    if gb < 10:
        return "8"
    if gb < 14:
        return "12"
    if gb < 20:
        return "16"
    if gb < 30:
        return "24"
    return "32+"


def system_snapshot(gpu: GpuInfo | None = None) -> dict[str, Any]:
    vm = psutil.virtual_memory()
    try:
        disk_free = shutil.disk_usage(ROOT).free / 1e9
    except Exception:
        disk_free = 0.0
    g = gpu or query_gpu()
    d = asdict(g)
    d["tier"] = vram_tier(g.total_mb)
    return {
        "gpu": d,
        "ram_total_mb": int(vm.total / 1024 / 1024),
        "ram_used_mb": int((vm.total - vm.available) / 1024 / 1024),
        "ram_free_mb": int(vm.available / 1024 / 1024),
        "cpu_percent": psutil.cpu_percent(interval=None),
        "disk_free_gb": round(disk_free, 1),
        "timestamp": time.time(),
    }


class GpuMonitor:
    """Samples nvidia-smi in the background so requests never pay for a subprocess call."""

    def __init__(self, interval: float = 2.0) -> None:
        self.interval = interval
        self.latest: GpuInfo = GpuInfo(available=False)
        self.snapshot: dict[str, Any] = system_snapshot(self.latest)
        self.history: collections.deque[tuple[float, int, int]] = collections.deque(maxlen=90)  # (ts, used_mb, util)
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        await self.refresh()
        self._task = asyncio.create_task(self._loop(), name="gpu-monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def refresh(self, with_processes: bool = False) -> GpuInfo:
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, query_gpu, with_processes)
        self.latest = info
        self.snapshot = await loop.run_in_executor(None, system_snapshot, info)
        self.history.append((time.time(), info.used_mb, info.util))
        return info

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.interval)
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(self.interval)

    async def free_mb_now(self) -> int:
        info = await self.refresh()
        return info.free_mb if info.available else 1 << 20
