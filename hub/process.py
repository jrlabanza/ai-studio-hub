"""Runs each tool as a child process in its own environment, exactly like its own launcher would.

Two backends:

* **native** - the tool's own Python environment (``.venv``), the way its ``Run.bat`` / ``start.sh``
  starts it. On Windows the process is tied to the hub's job object so it dies with the hub.
* **docker** (Linux) - the tool's container from its ``linux/compose.yml``. The hub runs
  ``docker compose up`` *attached* so the container's log streams into the hub and the child exits
  when the container does; stopping is an explicit ``docker compose stop`` (the container gets its
  30 s grace period to unload models).

A tool is *stopped*, *starting*, *running*, *stopping* or in *error*. A copy the user started by
hand (for example with the tool's own Run.bat, or ``ai run <tool>`` on Linux) is adopted instead of
duplicated: a native copy is proxied to but never killed; a container is simply taken over, since
there is only ever one container per tool.
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

from . import docker as dk
from . import winjob
from .config import LOGS_DIR, tool_cfg, tool_dir
from .tools import Helper, ToolSpec

if TYPE_CHECKING:
    from .core import Hub

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
IS_WINDOWS = sys.platform == "win32"


def _popen_kwargs() -> dict[str, Any]:
    """Children get their own process group / session so a Ctrl+C in the hub's window reaches the hub
    first, which then stops them in an orderly way."""
    if IS_WINDOWS:
        return {"creationflags": _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


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
        self.external = False           # running copy we did not start (adopted, never killed)
        self.backend = ""               # native | docker (decided at start)
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
        self._compose_profiles: tuple[str, ...] = ()
        # Set when the orchestrator stopped this studio to free VRAM for another one: until then its
        # entrance does not start it again on its own (a tab left open keeps polling), only the user does.
        self.held_until = 0.0

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
    def held(self) -> bool:
        return self.state in ("stopped", "error") and time.time() < self.held_until

    def held_by(self) -> str:
        owner = self.hub.orchestrator.owner
        return self.hub.tools[owner].spec.name if owner and owner in self.hub.tools and owner != self.id else "another studio"

    @property
    def is_docker(self) -> bool:
        return self.backend == "docker"

    @property
    def pid(self) -> int | None:
        if self.is_docker:
            return dk.container_pid(self.spec.container_name()) if self.state in ("running", "starting") else None
        return self.proc.pid if self.proc and self.proc.poll() is None else None

    def owns_pid(self, pid: int) -> bool:
        """True when ``pid`` belongs to this tool (its process tree, or its container's)."""
        root = self.pid
        if not root:
            return False
        if pid == root:
            return True
        try:
            return any(c.pid == pid for c in psutil.Process(root).children(recursive=True))
        except psutil.Error:
            return False

    def describe(self) -> dict[str, Any]:
        tdir = self.tool_dir
        installed, why = self.spec.installed(tdir)
        model_ok, model_why = self.spec.model_present(tdir) if installed else (False, "")
        backend = self.backend or self.spec.backend(tdir)
        return {
            **self.spec.describe(),
            "state": self.state, "error": self.error, "external": self.external, "enabled": self.enabled,
            "backend": backend, "container": self.spec.container_name() if backend == "docker" else None,
            "port": self.port, "proxy_port": self.cfg.get("proxy_port"), "pid": self.pid,
            "port_fixed": backend == "docker",
            "generation": self.generation, "started_at": self.started_at, "uptime_s": round(time.time() - self.started_at)
            if self.state == "running" and self.started_at else 0,
            "installed": installed, "install_note": why, "model_present": model_ok, "model_note": model_why,
            "dir": str(tdir), "outputs_dir": str(self.spec.outputs_dir(tdir)),
            "pinned": bool(self.cfg.get("pinned")), "autostart": bool(self.cfg.get("autostart")),
            "last_exit_code": self.last_exit_code, "helpers": [h.name for h, _ in self.helper_procs],
            "supports_unload": self.spec.supports_unload,
            "held": self.held, "held_by": self.held_by() if self.held else "",
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
            tdir = self.tool_dir
            ok, why = self.spec.installed(tdir)
            if not ok:
                self.state, self.error = "error", why
                self._publish()
                return False
            self.backend = self.spec.backend(tdir)
            self.held_until = 0.0
            self.state, self.error = "starting", ""
            self._publish()
            cfg = self.cfg
            loop = asyncio.get_running_loop()
            wanted = self.spec.resolved_port(tdir, cfg) or self.port or 8000

            # A copy started by hand? Adopt it rather than starting a second one on the same GPU.
            answers = await loop.run_in_executor(None, port_answers, wanted)
            if answers and await self.probe(wanted):
                container_running = self.is_docker and await loop.run_in_executor(
                    None, dk.container_state, self.spec.container_name()) == "running"
                if not container_running:
                    self.port, self.external = wanted, True
                    self.state, self.started_at = "running", time.time()
                    self.generation += 1
                    self._note(f"{self.spec.name} was already running on port {wanted} - using that copy")
                    self._publish()
                    return True
                self._note(f"{self.spec.name}'s container is already running on port {wanted} - taking it over")
            elif answers:
                self.state, self.error = "error", f"Port {wanted} is in use by something that is not {self.spec.name}"
                self._note(self.error, "error")
                self._publish()
                return False

            try:
                LOGS_DIR.mkdir(parents=True, exist_ok=True)
                self._log_fh = open(self.log_path, "a", encoding="utf-8", errors="replace")
                notes = await loop.run_in_executor(None, self.spec.pre_launch, tdir, cfg)
                for note in notes or []:
                    self._note(f"{self.spec.name}: {note}", "warn" if "failed" in note else "info")
                if self.is_docker:
                    port = wanted
                    self.proc, in_job = await loop.run_in_executor(None, self._launch_docker, tdir, cfg)
                else:
                    port = await loop.run_in_executor(None, pick_free_port, wanted)
                    self.proc, in_job = await loop.run_in_executor(None, self._launch_native, tdir, cfg, port)
            except Exception as exc:
                self.state, self.error = "error", f"Could not start: {exc}"
                self._note(self.error, "error")
                self._publish()
                return False
            self.port, self.external = port, False
            if not in_job and IS_WINDOWS:
                self._log("[hub] note: could not tie this process to the hub's job object; it is stopped explicitly on exit")
            threading.Thread(target=self._reader, args=(self.proc, ""), daemon=True, name=f"log-{self.id}").start()
            if not self.is_docker:
                self._start_helpers(cfg)
            self._note(f"Starting {self.spec.name} on port {port} ({'container ' + self.spec.container_name() if self.is_docker else 'pid ' + str(self.proc.pid)})")

            deadline = time.time() + self.spec.startup_timeout
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    code = self._exit_code(self.proc)
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
                    asyncio.get_running_loop().create_task(self.hub.orchestrator.on_started(self))
                    return True
                await asyncio.sleep(1.0)
            self.state, self.error = "error", f"{self.spec.name} did not answer within {int(self.spec.startup_timeout)} s"
            self._note(self.error, "error")
            await self._kill()
            self._publish()
            return False

    # ------------------------------------------------------------------ launching
    def _launch_native(self, tdir: Path, cfg: dict[str, Any], port: int) -> tuple[subprocess.Popen, bool]:
        argv = self.spec.argv(tdir, port, cfg)
        env = {**os.environ, **self.spec.env(tdir, cfg), "AI_HUB_URL": f"http://127.0.0.1:{self.hub.settings.hub_port}"}
        self._log(f"[hub] {time.strftime('%Y-%m-%d %H:%M:%S')} starting: {' '.join(argv)}")
        return winjob.popen_in_job(argv, cwd=str(tdir), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                   **_popen_kwargs())

    def _launch_docker(self, tdir: Path, cfg: dict[str, Any]) -> tuple[subprocess.Popen, bool]:
        services, profiles = self.spec.docker_services(tdir, cfg)
        self._compose_profiles = profiles
        cmd = dk.compose(self.spec.compose_file(tdir), profiles, self.spec.compose_overrides(tdir))
        if not cmd:
            raise RuntimeError("Docker is not reachable")
        argv = [*cmd, "up", "--no-log-prefix", "--no-color", "--no-build"]
        if len(services) > 1:
            argv.append("--abort-on-container-exit")
        argv += services
        env = {**os.environ, **dk.compose_env(), **self.spec.docker_env(tdir, cfg),
               "AI_HUB_URL": f"http://127.0.0.1:{self.hub.settings.hub_port}"}
        self._log(f"[hub] {time.strftime('%Y-%m-%d %H:%M:%S')} starting: {' '.join(argv)}")
        proc = subprocess.Popen(argv, cwd=str(tdir / "linux"), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                **_popen_kwargs())
        dk.forget("pid:")
        return proc, False

    def _exit_code(self, proc: subprocess.Popen) -> int | None:
        """The tool's exit code: the container's for docker (the attached ``compose up`` returns 0 whenever
        the containers simply stopped), the child's otherwise."""
        if self.is_docker:
            code = dk.container_exit_code(self.spec.container_name())
            return code if code is not None else proc.returncode
        return proc.returncode

    def _start_helpers(self, cfg: dict[str, Any]) -> None:
        for helper in self.spec.helpers(self.tool_dir, cfg):
            try:
                p, _in_job = winjob.popen_in_job(helper.argv, cwd=str(helper.cwd), env={**os.environ, **helper.env},
                                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                 text=True, encoding="utf-8", errors="replace", **_popen_kwargs())
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
        port_re = re.compile(self.spec.port_regex) if (self.spec.port_regex and not tag and not self.is_docker) else None
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
        proc.wait()
        code = self._exit_code(proc)
        if not tag:
            loop = self.hub.bus.loop
            if loop and loop.is_running():
                loop.call_soon_threadsafe(self._on_exit, proc, code)

    def _on_exit(self, proc: subprocess.Popen, code: int | None) -> None:
        if proc is not self.proc:
            return
        self.last_exit_code = code
        self.proc = None
        was = self.state
        self._stop_helpers()
        dk.forget("pid:")
        stopped_outside = self.is_docker and code in (0, 137, 143)   # docker stop / ai stop / Ctrl+C elsewhere
        if was == "stopping":
            self.state = "stopped"
        elif code in self.spec.restart_exit_codes and self.restart_count < 5:
            self.restart_count += 1
            self.state = "stopped"
            self._note(f"{self.spec.name} asked to be restarted (exit code {code}) - restarting", "warn")
            asyncio.get_running_loop().create_task(self.start())
        elif was in ("running", "starting") and stopped_outside:
            self.state, self.error = "stopped", ""
            self._note(f"{self.spec.name} was stopped outside the hub")
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
        loop = asyncio.get_running_loop()
        if self.is_docker:
            tdir = self.tool_dir
            ok, info = await loop.run_in_executor(None, dk.stop_container, self.spec.compose_file(tdir),
                                                  self._compose_profiles, 150.0, self.spec.compose_overrides(tdir))
            if not ok:
                self._log(f"[hub] docker compose stop: {info}")
            if proc and proc.poll() is None:
                # The attached `compose up` exits by itself once the container is down; give it a moment.
                for _ in range(30):
                    if proc.poll() is not None:
                        break
                    await asyncio.sleep(0.5)
                if proc.poll() is None:
                    await loop.run_in_executor(None, terminate_tree, proc.pid)
            dk.forget("pid:")
            return
        if proc and proc.poll() is None:
            await loop.run_in_executor(None, terminate_tree, proc.pid)
        await loop.run_in_executor(None, self._stop_helpers)

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
