"""GPU / RAM telemetry without importing torch - for NVIDIA and AMD cards alike.

Readers, in order of preference:

* ``nvidia-smi`` (NVIDIA, Windows + Linux, ~30 ms per call);
* ``rocm-smi`` / ``amd-smi`` (AMD on Linux);
* Windows performance counters (any vendor: ``GPU Adapter Memory`` / ``GPU Process Memory``), the path
  AMD cards take on Windows, where ROCm ships no SMI tool.

See docs/gpu.md.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import collections
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import psutil

from .config import ROOT

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
IS_WINDOWS = sys.platform == "win32"


def _which(*names: str) -> str | None:
    for n in names:
        exe = shutil.which(n)
        if exe:
            return exe
    return None


def _nvidia_smi_path() -> str | None:
    exe = shutil.which("nvidia-smi")
    if exe:
        return exe
    for cand in (r"C:\Windows\System32\nvidia-smi.exe",
                 r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
        if os.path.exists(cand):
            return cand
    return None


def _run(argv: list[str], timeout: float = 4.0) -> str | None:
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, creationflags=_CREATE_NO_WINDOW)
        if out.returncode != 0:
            return None
        return out.stdout.strip()
    except Exception:
        return None


def _num(v: Any, default: float = 0.0) -> float:
    s = str(v).strip()
    if not s or s.startswith("[N/A]") or s == "N/A":
        return default
    try:
        return float(re.sub(r"[^\d.\-]", "", s) or default)
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
    vendor: str = ""                 # nvidia | amd | ""
    backend: str = ""                # cuda | rocm | ""
    source: str = ""                 # nvidia-smi | rocm-smi | amd-smi | windows-counters
    processes: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------------- NVIDIA
_SMI = _nvidia_smi_path()


def _smi(args: list[str], timeout: float = 4.0) -> str | None:
    if not _SMI:
        return None
    return _run([_SMI, *args], timeout)


def _query_nvidia(with_processes: bool, light: bool = False) -> GpuInfo | None:
    if light:
        # During a render: memory only. Every nvidia-smi call is an RPC into the GPU's GSP firmware, and on the
        # 595 open driver a query under full load has crashed that firmware (Xid 120); ask as little as possible.
        line = _smi(["--query-gpu=name,memory.total,memory.used,memory.free", "--format=csv,noheader,nounits"])
        if not line:
            return None
        parts = [p.strip() for p in line.splitlines()[0].split(",")]
        try:
            info = GpuInfo(available=True, name=parts[0], total_mb=int(_num(parts[1])), used_mb=int(_num(parts[2])),
                           free_mb=int(_num(parts[3])))
        except Exception:
            info = GpuInfo(available=True, name=parts[0] if parts else "GPU")
        info.vendor, info.backend, info.source = "nvidia", "cuda", "nvidia-smi"
        return info
    line = _smi(["--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu,power.draw,"
                 "driver_version,compute_cap", "--format=csv,noheader,nounits"])
    if not line:
        return None
    parts = [p.strip() for p in line.splitlines()[0].split(",")]
    try:
        info = GpuInfo(available=True, name=parts[0], total_mb=int(_num(parts[1])), used_mb=int(_num(parts[2])),
                       free_mb=int(_num(parts[3])), util=int(_num(parts[4])), temp=int(_num(parts[5])),
                       power_w=round(_num(parts[6]), 1), driver=parts[7] if len(parts) > 7 else "",
                       compute_cap=parts[8] if len(parts) > 8 else "")
    except Exception:
        info = GpuInfo(available=True, name=parts[0] if parts else "GPU")
    info.vendor, info.backend, info.source = "nvidia", "cuda", "nvidia-smi"
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


# ---------------------------------------------------------------------------------- AMD on Linux
_ROCM_SMI = None if IS_WINDOWS else _which("rocm-smi", "/opt/rocm/bin/rocm-smi")
_AMD_SMI = None if IS_WINDOWS else _which("amd-smi", "/opt/rocm/bin/amd-smi")


def _first_card(d: Any) -> dict[str, Any]:
    if isinstance(d, dict):
        for k, v in d.items():
            if isinstance(v, dict) and (k.startswith("card") or k.startswith("gpu")):
                return v
        return d
    if isinstance(d, list) and d and isinstance(d[0], dict):
        return d[0]
    return {}


def parse_rocm_smi(text: str) -> GpuInfo | None:
    """``rocm-smi --showmeminfo vram --showuse --showtemp --showpower --showproductname --showdriverversion --json``."""
    try:
        d = json.loads(text)
    except Exception:
        return None
    card = _first_card(d)
    if not card:
        return None

    def pick(*keys: str) -> Any:
        for k in keys:
            for kk, v in card.items():
                if kk.lower() == k.lower():
                    return v
        for k in keys:
            for kk, v in card.items():
                if k.lower() in kk.lower():
                    return v
        return None

    total_b = _num(pick("VRAM Total Memory (B)"))
    used_b = _num(pick("VRAM Total Used Memory (B)"))
    name = str(pick("Card Series", "Card series", "Card model", "GPU name", "Device Name") or "AMD GPU")
    info = GpuInfo(available=True, name=name, total_mb=int(total_b / 1024 / 1024), used_mb=int(used_b / 1024 / 1024),
                   free_mb=int((total_b - used_b) / 1024 / 1024), util=int(_num(pick("GPU use (%)"))),
                   temp=int(_num(pick("Temperature (Sensor edge) (C)", "Temperature (Sensor junction) (C)"))),
                   power_w=round(_num(pick("Average Graphics Package Power (W)", "Current Socket Graphics Package Power (W)")), 1),
                   driver=str(d.get("system", {}).get("Driver version", "") if isinstance(d.get("system"), dict) else ""),
                   vendor="amd", backend="rocm", source="rocm-smi")
    return info


def parse_rocm_smi_pids(text: str) -> list[dict[str, Any]]:
    """``rocm-smi --showpids --json`` -> [{pid, used_mb, name}]."""
    out: list[dict[str, Any]] = []
    try:
        d = json.loads(text)
    except Exception:
        return out
    sysd = d.get("system") if isinstance(d, dict) else None
    for k, v in (sysd or {}).items():
        if not isinstance(v, (dict, str)) or not str(k).startswith("PID"):
            continue
        pid = int(_num(str(k).replace("PID", "")))
        name, vram = "", None
        if isinstance(v, dict):
            name = str(v.get("Process name", v.get("Process Name", "")))
            vr = v.get("VRAM Used", v.get("VRAM used"))
            vram = int(_num(vr) / 1024 / 1024) if vr not in (None, "") else None
        elif isinstance(v, str):
            parts = [p.strip() for p in v.split(",")]
            if parts:
                name = parts[0]
            if len(parts) > 2:
                vram = int(_num(parts[2]) / 1024 / 1024)
        out.append({"pid": pid, "used_mb": vram, "name": name})
    return out


def _query_amd_linux(with_processes: bool) -> GpuInfo | None:
    if _ROCM_SMI:
        text = _run([_ROCM_SMI, "--showmeminfo", "vram", "--showuse", "--showtemp", "--showpower", "--showproductname",
                     "--showdriverversion", "--json"], timeout=5)
        info = parse_rocm_smi(text) if text else None
        if info:
            if with_processes:
                pids = _run([_ROCM_SMI, "--showpids", "--json"], timeout=5)
                if pids:
                    info.processes = parse_rocm_smi_pids(pids)
            return info
    if _AMD_SMI:
        text = _run([_AMD_SMI, "metric", "--mem-usage", "--usage", "--temperature", "--power", "--json"], timeout=5)
        try:
            d = json.loads(text or "")
            card = _first_card(d)
            mem = card.get("mem_usage", {})
            total = _num((mem.get("total_vram") or {}).get("value", 0))
            used = _num((mem.get("used_vram") or {}).get("value", 0))
            unit = str((mem.get("total_vram") or {}).get("unit", "MB")).upper()
            scale = 1024 if unit.startswith("G") else 1
            info = GpuInfo(available=True, name=str(card.get("gpu", "AMD GPU")), total_mb=int(total * scale),
                           used_mb=int(used * scale), free_mb=int((total - used) * scale),
                           util=int(_num((card.get("usage", {}).get("gfx_activity") or {}).get("value", 0))),
                           temp=int(_num((card.get("temperature", {}).get("edge") or {}).get("value", 0))),
                           power_w=round(_num((card.get("power", {}).get("socket_power") or {}).get("value", 0)), 1),
                           vendor="amd", backend="rocm", source="amd-smi")
            return info
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------------- Windows counters
_PS = _which("pwsh", "powershell") if IS_WINDOWS else None
_WIN_PS = r"""
$ErrorActionPreference='SilentlyContinue'
$a = Get-CimInstance Win32_VideoController | Where-Object { $_.Name -notmatch 'Microsoft|Virtual|Remote|Basic' } | Sort-Object AdapterRAM -Descending | Select-Object -First 1
$total = 0
try { $keys = Get-ChildItem 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}' | Where-Object { $_.PSChildName -match '^\d{4}$' }
  foreach ($k in $keys) { $p = Get-ItemProperty $k.PSPath; if ($p.'HardwareInformation.qwMemorySize') { $m = [int64]$p.'HardwareInformation.qwMemorySize'; if ($m -gt $total) { $total = $m } } } } catch {}
if (-not $total -and $a.AdapterRAM) { $total = [int64]$a.AdapterRAM }
$used = 0; $util = 0
try { $used = ((Get-Counter '\GPU Adapter Memory(*)\Dedicated Usage').CounterSamples | Measure-Object CookedValue -Sum).Sum } catch {}
try { $util = ((Get-Counter '\GPU Engine(*engtype_3D)\Utilization Percentage').CounterSamples | Measure-Object CookedValue -Sum).Sum } catch {}
$procs = @()
if ($env:HUB_GPU_PROCS -eq '1') { try { $procs = (Get-Counter '\GPU Process Memory(*)\Dedicated Usage').CounterSamples | Where-Object { $_.CookedValue -gt 0 } | ForEach-Object { @{ pid = [int](($_.InstanceName -split '_')[1]); bytes = [int64]$_.CookedValue } } } catch {} }
@{ name = [string]$a.Name; driver = [string]$a.DriverVersion; total = [int64]$total; used = [int64]$used; util = [int](([double]$util) -as [int]); procs = $procs } | ConvertTo-Json -Compress -Depth 3
"""


def _query_windows_counters(with_processes: bool) -> GpuInfo | None:
    if not _PS:
        return None
    env = {**os.environ, "HUB_GPU_PROCS": "1" if with_processes else "0"}
    try:
        out = subprocess.run([_PS, "-NoProfile", "-NonInteractive", "-Command", _WIN_PS], capture_output=True, text=True,
                             timeout=12, creationflags=_CREATE_NO_WINDOW, env=env)
        d = json.loads(out.stdout.strip() or "{}")
    except Exception:
        return None
    if not d.get("name"):
        return None
    name = str(d["name"])
    total = int(_num(d.get("total")) / 1024 / 1024)
    used = int(_num(d.get("used")) / 1024 / 1024)
    vendor = "amd" if re.search(r"AMD|Radeon", name, re.I) else ("nvidia" if "NVIDIA" in name else "")
    info = GpuInfo(available=True, name=name, total_mb=total, used_mb=used, free_mb=max(0, total - used),
                   util=int(min(100, _num(d.get("util")))), driver=str(d.get("driver", "")), vendor=vendor,
                   backend="rocm" if vendor == "amd" else ("cuda" if vendor == "nvidia" else ""), source="windows-counters")
    for p in d.get("procs") or []:
        try:
            pid = int(p["pid"])
            info.processes.append({"pid": pid, "used_mb": int(_num(p["bytes"]) / 1024 / 1024),
                                   "name": psutil.Process(pid).name() if psutil.pid_exists(pid) else ""})
        except Exception:
            continue
    return info


# ---------------------------------------------------------------------------------- public
def query_gpu(with_processes: bool = False, light: bool = False) -> GpuInfo:
    info = _query_nvidia(with_processes and not light, light=light)
    if info:
        return info
    if not IS_WINDOWS:
        info = _query_amd_linux(with_processes)
        if info:
            return info
    else:
        info = _query_windows_counters(with_processes)
        if info:
            return info
    return GpuInfo(available=False)


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
    """Samples the card in the background so requests never pay for a subprocess call."""

    BUSY_INTERVAL = 30.0                 # while a studio renders: sample rarely, memory only
    BUSY_FLAG = Path(os.environ.get("AI_CACHE") or Path.home() / ".cache" / "ai-tools") / "gpu-busy"

    def __init__(self, interval: float = 2.0) -> None:
        self.interval = interval
        self.busy = lambda: False        # set by the hub: is any studio rendering right now?
        self.latest: GpuInfo = GpuInfo(available=False)
        self.snapshot: dict[str, Any] = system_snapshot(self.latest)
        self.history: collections.deque[tuple[float, int, int]] = collections.deque(maxlen=90)  # (ts, used_mb, util)
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        await self.refresh()
        # The Windows counter path costs ~1 s per call: sample it less often.
        if self.latest.source == "windows-counters":
            self.interval = max(self.interval, 4.0)
        self._task = asyncio.create_task(self._loop(), name="gpu-monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def refresh(self, with_processes: bool = False, light: bool = False) -> GpuInfo:
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, query_gpu, with_processes, light)
        self.latest = info
        self.snapshot = await loop.run_in_executor(None, system_snapshot, info)
        self.history.append((time.time(), info.used_mb, info.util))
        return info

    def _flag(self, on: bool) -> None:
        """A file other GPU pollers (the desktop launcher) check, so they too leave the card alone mid-render."""
        try:
            if on:
                self.BUSY_FLAG.parent.mkdir(parents=True, exist_ok=True)
                self.BUSY_FLAG.touch()
            elif self.BUSY_FLAG.exists():
                self.BUSY_FLAG.unlink()
        except OSError:
            pass

    async def _loop(self) -> None:
        while True:
            try:
                busy = False
                try:
                    busy = bool(self.busy())
                except Exception:
                    pass
                self._flag(busy)
                if busy and self.latest.source == "nvidia-smi":
                    await asyncio.sleep(self.BUSY_INTERVAL)
                    if bool(self.busy()):
                        self._flag(True)
                        await self.refresh(light=True)
                        continue
                await asyncio.sleep(self.interval)
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(self.interval)

    async def free_mb_now(self) -> int:
        info = await self.refresh()
        return info.free_mb if info.available else 1 << 20
