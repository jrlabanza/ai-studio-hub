"""Docker helpers for the Linux packaging of the studios.

On Linux every studio ships a ``linux/`` directory (Dockerfile, ``compose.yml``, ``run.sh``,
``stop.sh`` ...) and runs in a container with its own Python and PyTorch build. The hub drives
those containers through ``docker compose`` directly - it does *not* go through ``run.sh``, because
``run.sh`` enforces "one tool at a time" by stopping every other container first, and the hub's
auto-loader makes that decision itself (unload models first, stop processes only when the card is
still too full).

All calls here are synchronous and cheap; the process layer runs them in an executor.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
IS_LINUX = sys.platform.startswith("linux")

_docker_cmd: list[str] | None = None
_docker_checked = 0.0
_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl: float, compute):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = compute()
    _cache[key] = (now, val)
    return val


def forget(key_prefix: str = "") -> None:
    for k in list(_cache):
        if k.startswith(key_prefix):
            _cache.pop(k, None)


def _run(argv: list[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, creationflags=_CREATE_NO_WINDOW)


def docker() -> list[str] | None:
    """``["docker"]`` or ``["sudo", "-n", "docker"]`` (the docker group only takes effect after a new
    login) - or None when no daemon can be reached. Re-checked every 30 s when unavailable."""
    global _docker_cmd, _docker_checked
    if _docker_cmd is not None:
        return _docker_cmd
    if time.time() - _docker_checked < 30:
        return None
    _docker_checked = time.time()
    exe = shutil.which("docker")
    if not exe:
        return None
    for cand in ([exe], ["sudo", "-n", exe]):
        try:
            if _run([*cand, "info"], timeout=15).returncode == 0:
                _docker_cmd = cand
                return cand
        except Exception:
            continue
    return None


def available() -> bool:
    return docker() is not None


def compose_env() -> dict[str, str]:
    """The variables the studios' compose files expect (the same ones their run.sh exports)."""
    cache = os.environ.get("AI_CACHE") or str(Path.home() / ".cache" / "ai-tools")
    for sub in ("hf", "torch", "xdg", "mpl", "numba", "gradio"):
        try:
            Path(cache, sub).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    env = {"AI_CACHE": cache}
    if hasattr(os, "getuid"):
        env["AI_UID"] = str(os.getuid())
        env["AI_GID"] = str(os.getgid())
        # ROCm containers join the host's video/render groups to reach /dev/kfd and /dev/dri.
        try:
            import grp

            for name, var in (("video", "AI_VIDEO_GID"), ("render", "AI_RENDER_GID")):
                try:
                    env[var] = str(grp.getgrnam(name).gr_gid)
                except KeyError:
                    env[var] = "44" if name == "video" else "992"
        except ImportError:
            pass
    return env


def compose(compose_file: Path, profiles: tuple[str, ...] = (), overrides: tuple[Path, ...] = ()) -> list[str] | None:
    dk = docker()
    if not dk:
        return None
    cmd = [*dk]
    if dk[0] == "sudo":
        cmd = ["sudo", "-n", "--preserve-env=AI_UID,AI_GID,AI_VIDEO_GID,AI_RENDER_GID,AI_CACHE,AI_BIND,AI_HUB_URL", *dk[2:]]
    cmd += ["compose", "-f", str(compose_file)]
    for o in overrides:
        cmd += ["-f", str(o)]
    for p in profiles:
        cmd += ["--profile", p]
    return cmd


def image_exists(image: str) -> bool:
    def compute() -> bool:
        dk = docker()
        if not dk:
            return False
        try:
            return _run([*dk, "image", "inspect", "--format", "{{.Id}}", image], timeout=15).returncode == 0
        except Exception:
            return False
    return _cached(f"image:{image}", 60.0, compute)


def container_state(name: str) -> str:
    """running | exited | created | missing | unknown"""
    dk = docker()
    if not dk:
        return "unknown"
    try:
        r = _run([*dk, "inspect", "--format", "{{.State.Status}}", name], timeout=10)
    except Exception:
        return "unknown"
    if r.returncode != 0:
        return "missing"
    return r.stdout.strip() or "unknown"


def container_exit_code(name: str) -> int | None:
    dk = docker()
    if not dk:
        return None
    try:
        r = _run([*dk, "inspect", "--format", "{{.State.ExitCode}}", name], timeout=10)
        return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().lstrip("-").isdigit() else None
    except Exception:
        return None


def container_pid(name: str) -> int | None:
    """Host PID of the container's init process (0 when not running)."""
    def compute() -> int | None:
        dk = docker()
        if not dk:
            return None
        try:
            r = _run([*dk, "inspect", "--format", "{{.State.Pid}}", name], timeout=10)
            pid = int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else 0
            return pid or None
        except Exception:
            return None
    return _cached(f"pid:{name}", 10.0, compute)


def stop_container(compose_file: Path, profiles: tuple[str, ...] = (), timeout: float = 120.0,
                   overrides: tuple[Path, ...] = ()) -> tuple[bool, str]:
    cmd = compose(compose_file, profiles, overrides)
    if not cmd:
        return False, "docker is not reachable"
    try:
        r = subprocess.run([*cmd, "stop"], capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, **compose_env()}, creationflags=_CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False, "docker compose stop timed out"
    except Exception as exc:
        return False, str(exc)
    return r.returncode == 0, (r.stderr or r.stdout).strip()[-300:]


def read_run_sh(linux_dir: Path) -> dict[str, Any]:
    """``TOOL=`` / ``PORT=`` and the display name from a studio's ``linux/run.sh``."""
    info: dict[str, Any] = {}
    f = linux_dir / "run.sh"
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return info
    m = re.search(r'^TOOL="([^"]+)"', text, re.M)
    if m:
        info["tool"] = m.group(1)
    m = re.search(r'\bPORT=(\d+)', text)
    if m:
        info["port"] = int(m.group(1))
    lines = text.splitlines()
    if len(lines) > 1:
        m = re.match(r"#\s*Start (.*?) in a container\.?$", lines[1].strip())
        if m:
            info["name"] = m.group(1)
    return info


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out
