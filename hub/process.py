"""Runs each tool as a child process in its own environment, exactly like its own launcher would.

A tool is *stopped*, *starting*, *running*, *stopping* or in *error*. A copy the user started by
hand (for example with the tool's own Run.bat) is adopted instead of duplicated - the hub proxies to
it but never kills it.
"""
from __future__ import annotations

import asyncio
import collections
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import psutil

from . import winjob
from .config import LOGS_DIR, tool_cfg, tool_dir
from .tools import Helper, ToolSpec

if TYPE_CHECKING:
    from .core import Hub

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def port_answers(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as c:
        c.settimeout(0.3)
        return c.connect_ex((host, port)) == 0


def pick_free_port(preferred: int, tries: int = 30) -> int:
    for p in range(preferred, preferred + tries):
        if port_answers(p):
            continue
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError(f"No free port between {preferred} and {preferred + tries}")


def terminate_tree(pid: int, timeout: float = 12.0) -> None:
    """Terminate a process and everything it spawned (ComfyUI, workers ...)."""
    try:
        root = psutil.Process(pid)
    except psutil.Error:
        return
    procs = []
    try:
        procs = root.children(recursive=True)
    except psutil.Error:
        pass
    procs.append(root)
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(procs, timeout=timeout)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(alive, timeout=5)


class ManagedProcess:
    def __init__(self, spec: ToolSpec, hub: "Hub") -> None:
        self.spec = spec
        self.hub = hub
        self.state = "stopped"          # stopped | starting | running | stopping | error
        self.error = ""
        self.external = False           # running copy we did not start (adopted)
        self.proc: subprocess.Popen | None = None
        self.helper_procs: list[tuple[Helper, subprocess.Popen]] = []
        self.port: int = int(tool_cfg(spec.id).get("port") or 0)
        self.generation = 0             # increments on every successful start (the shell reloads its iframe)
        self.started_at = 0.0
        self.last_exit_code: int | None = None
        self.restart_count = 0
        self.log_lines: collections.deque[str] = collections.deque(maxlen=500)
        self.log_path = LOGS_DIR / f"{spec.id}.log"
        self._lock = asyncio.Lock()
        self._log_fh = None

    # ------------------------------------------------------------------ properties
    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def cfg(self) -> dict[str, Any]:
        return tool_cfg(self.spec.id)

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True))

    @property
    def tool_dir(self) -> Path:
        return tool_dir(self.spec.id)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc and self.proc.poll() is None else None

    def describe(self) -> dict[str, Any]:
        installed, why = self.spec.installed(self.tool_dir)
        model_ok, model_why = self.spec.model_present(self.tool_dir) if installed else (False, "")
        return {
            **self.spec.describe(),
            "state": self.state, "error": self.error, "external": self.external, "enabled": self.enabled,
            "port": self.port, "proxy_port": self.cfg.get("proxy_port"), "pid": self.pid,
            "generation": self.generation, "started_at": self.started_at, "uptime_s": round(time.time() - self.started_at)
            if self.state == "running" and self.started_at else 0,
            "installed": installed, "install_note": why, "model_present": model_ok, "model_note": model_why,
            "dir": str(self.tool_dir), "outputs_dir": str(self.spec.outputs_dir(self.tool_dir)),
            "pinned": bool(self.cfg.get("pinned")), "autostart": bool(self.cfg.get("autostart")),
            "last_exit_code": self.last_exit_code, "helpers": [h.name for h, _ in self.helper_procs],
        }

    def tail(self, n: int = 200) -> list[str]:
        return list(self.log_lines)[-n:]

    # ------------------------------------------------------------------ logging
    def _log(self, line: str, tag: str = "") -> None:
        line = line.rstrip("\r\n")
        if not line:
            return
        if tag:
            line = f"[{tag}] {line}"
        self.log_lines.append(line)
        try:
            if self._log_fh:
                self._log_fh.write(line + "\n")
                self._log_fh.flush()
        except Exception:
            pass

    def _note(self, message: str, level: str = "info") -> None:
        self._log(f"[hub] {message}")
        self.hub.bus.log_threadsafe(message, tool=self.id, level=level)

    # ------------------------------------------------------------------ health
    async def probe(self, port: int | None = None) -> bool:
        port = port or self.port
        if not port:
            return False
        try:
            r = await self.hub.http.get(f"http://127.0.0.1:{port}{self.spec.health_path}", timeout=3.0)
            body: Any = None
            try:
                body = r.json()
            except Exception:
                body = None
            return self.spec.health_ok(r.status_code, body)
        except Exception:
            return False

    # ------------------------------------------------------------------ lifecycle
    async def ensure_running(self) -> bool:
        if self.state == "running":
            return True
        return await self.start()

    async def start(self) -> bool:
        async with self._lock:
            if self.state == "running":
                return True
            if not self.enabled:
                self.state, self.error = "stopped", "Disabled in Settings"
                return False
            ok, why = self.spec.installed(self.tool_dir)
            if not ok:
                self.state, self.error = "error", why
                self._publish()
                return False
            self.state, self.error = "starting", ""
            self._publish()
            cfg = self.cfg
            wanted = int(cfg.get("port") or self.port or 8000)
            loop = asyncio.get_running_loop()

            # A copy started by hand? Adopt it rather than starting a second one on the same GPU.
            if await loop.run_in_executor(None, port_answers, wanted) and await self.probe(wanted):
                self.port, self.external = wanted, True
                self.state, self.started_at = "running", time.time()
                self.generation += 1
                self._note(f"{self.spec.name} was already running on port {wanted} - using that copy")
                self._publish()
                return True

            try:
                port = await loop.run_in_executor(None, pick_free_port, wanted)
                LOGS_DIR.mkdir(parents=True, exist_ok=True)
                self._log_fh = open(self.log_path, "a", encoding="utf-8", errors="replace")
                notes = await loop.run_in_executor(None, self.spec.pre_launch, self.tool_dir, cfg)
                for note in notes or []:
                    self._note(f"{self.spec.name}: {note}", "warn" if "failed" in note else "info")
                argv = self.spec.argv(self.tool_dir, port, cfg)
                env = {**os.environ, **self.spec.env(self.tool_dir, cfg), "AI_HUB_URL": f"http://127.0.0.1:{self.hub.settings.hub_port}"}
                self._log(f"[hub] {time.strftime('%Y-%m-%d %H:%M:%S')} starting: {' '.join(argv)}")
                self.proc, in_job = await loop.run_in_executor(None, lambda: winjob.popen_in_job(
                    argv, cwd=str(self.tool_dir), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                    creationflags=_CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0))
            except Exception as exc:
                self.state, self.error = "error", f"Could not start: {exc}"
                self._note(self.error, "error")
                self._publish()
                return False
            self.port, self.external = port, False
            if not in_job:
                self._log("[hub] note: could not tie this process to the hub's job object; it is stopped explicitly on exit")
            threading.Thread(target=self._reader, args=(self.proc, ""), daemon=True, name=f"log-{self.id}").start()
            self._start_helpers(cfg)
            self._note(f"Starting {self.spec.name} on port {port} (pid {self.proc.pid})")

            deadline = time.time() + self.spec.startup_timeout
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    code = self.proc.returncode
                    tail = "\n".join(self.tail(6))
                    self.state, self.error = "error", f"{self.spec.name} exited during start-up (code {code}).\n{tail}"
                    self._note(f"{self.spec.name} exited during start-up with code {code}", "error")
                    self._stop_helpers()
                    self._publish()
                    return False
                if await self.probe(self.port):
                    self.state, self.started_at = "running", time.time()
                    self.generation += 1
                    self._note(f"{self.spec.name} is ready on port {self.port}")
                    self._publish()
                    return True
                await asyncio.sleep(1.0)
            self.state, self.error = "error", f"{self.spec.name} did not answer within {int(self.spec.startup_timeout)} s"
            self._note(self.error, "error")
            await self._kill()
            self._publish()
            return False

    def _start_helpers(self, cfg: dict[str, Any]) -> None:
        for helper in self.spec.helpers(self.tool_dir, cfg):
            try:
                p, _in_job = winjob.popen_in_job(helper.argv, cwd=str(helper.cwd), env={**os.environ, **helper.env},
                                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                 text=True, encoding="utf-8", errors="replace",
                                                 creationflags=_CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0)
            except Exception as exc:
                self._note(f"Helper {helper.name} could not start: {exc}", "warn")
                continue
            self.helper_procs.append((helper, p))
            threading.Thread(target=self._reader, args=(p, helper.name), daemon=True, name=f"log-{self.id}-{helper.name}").start()
            self._log(f"[hub] helper {helper.name} started (pid {p.pid})")

    def _stop_helpers(self) -> None:
        for _helper, p in self.helper_procs:
            if p.poll() is None:
                terminate_tree(p.pid, timeout=8)
        self.helper_procs = []

    def _reader(self, proc: subprocess.Popen, tag: str) -> None:
        port_re = re.compile(self.spec.port_regex) if (self.spec.port_regex and not tag) else None
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                if not line.strip():
                    continue
                self._log(line, tag)
                if port_re:
                    m = port_re.search(line)
                    if m:
                        port = int(m.group(1))
                        if port != self.port:
                            self.port = port
                            self._log(f"[hub] tool reports port {port}")
        except Exception:
            pass
        code = proc.wait()
        if not tag:
            loop = self.hub.bus.loop
            if loop and loop.is_running():
                loop.call_soon_threadsafe(self._on_exit, proc, code)

    def _on_exit(self, proc: subprocess.Popen, code: int) -> None:
        if proc is not self.proc:
            return
        self.last_exit_code = code
        self.proc = None
        was = self.state
        self._stop_helpers()
        if was == "stopping":
            self.state = "stopped"
        elif code in self.spec.restart_exit_codes and self.restart_count < 5:
            self.restart_count += 1
            self.state = "stopped"
            self._note(f"{self.spec.name} asked to be restarted (exit code {code}) - restarting", "warn")
            asyncio.get_running_loop().create_task(self.start())
        elif was in ("running", "starting"):
            self.state = "error" if code not in (0, None) else "stopped"
            self.error = f"{self.spec.name} exited unexpectedly (code {code})" if code else ""
            self._note(self.error or f"{self.spec.name} stopped", "error" if code else "info")
        else:
            self.state = "stopped"
        if self._log_fh:
            try:
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None
        self._publish()

    async def _kill(self) -> None:
        proc = self.proc
        if proc and proc.poll() is None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, terminate_tree, proc.pid)
        await asyncio.get_running_loop().run_in_executor(None, self._stop_helpers)

    async def stop(self, reason: str = "") -> None:
        async with self._lock:
            if self.external:
                # Not ours to kill: just stop proxying to it.
                self.external = False
                self.state, self.started_at = "stopped", 0.0
                self._note(f"Detached from the externally started {self.spec.name}")
                self._publish()
                return
            if self.state in ("stopped",) and not self.proc:
                return
            self.state = "stopping"
            self._publish()
            self._note(f"Stopping {self.spec.name}" + (f" ({reason})" if reason else ""))
            await self._kill()
            self.proc = None
            self.state, self.error, self.started_at = "stopped", "", 0.0
            self._publish()

    async def restart(self) -> bool:
        await self.stop("restart")
        return await self.start()

    def _publish(self) -> None:
        self.hub.bus.publish_threadsafe({"type": "tool", "tool": self.id, "state": self.state, "error": self.error})
