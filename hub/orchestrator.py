"""The auto-loader: one GPU, several studios, no manual model juggling.

Whenever a request that needs the GPU reaches a tool (a *claim*), the orchestrator first makes
room for it: it waits for jobs running elsewhere, unloads the other tools' models, and - only if
the card is still too full - stops the other tools' processes (each idle process still pins a
few hundred MB of CUDA context, which matters on an 8 GB card) and asks a local Ollama to drop its
models. Everything is measured with nvidia-smi rather than assumed, so on a 24 GB card two small
models can happily share the GPU while an 8 GB card runs strictly one model at a time.

It also warms up the tool you switch to (optional) and frees the GPU after idle periods.
"""
from __future__ import annotations

import asyncio
import collections
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from .config import load_settings
from .tools import Step, Summary

if TYPE_CHECKING:
    from .core import Hub
    from .process import ManagedProcess

OLLAMA_URL = "http://127.0.0.1:11434"


def idle_defaults(total_mb: int) -> tuple[float, float]:
    """(unload after N minutes, stop process after N minutes) chosen from the VRAM size."""
    gb = total_mb / 1024 if total_mb else 0
    if gb <= 0:
        return 30.0, 90.0
    if gb < 12:
        return 8.0, 25.0
    if gb < 20:
        return 15.0, 45.0
    return 30.0, 90.0


@dataclass
class ClaimResult:
    ok: bool
    tool: str
    actions: list[str] = field(default_factory=list)
    warning: str = ""
    waited_s: float = 0.0
    free_mb: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "tool": self.tool, "actions": self.actions, "warning": self.warning,
                "waited_s": round(self.waited_s, 1), "free_mb": self.free_mb}


class Orchestrator:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.owner: str | None = None
        self.owner_since = 0.0
        self.summaries: dict[str, Summary] = {}
        self.summary_ts: dict[str, float] = {}
        self.last_activity: dict[str, float] = {}
        self.focus: str | None = None
        self.focus_ts = 0.0
        self.pending: list[dict[str, Any]] = []
        self.recent_actions: collections.deque[dict[str, Any]] = collections.deque(maxlen=40)
        self._lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self._prepare_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._tasks = [asyncio.create_task(self._poll_loop(), name="orch-poll"),
                       asyncio.create_task(self._idle_loop(), name="orch-idle")]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._prepare_task:
            self._prepare_task.cancel()

    # ------------------------------------------------------------------ helpers
    @property
    def tools(self) -> dict[str, "ManagedProcess"]:
        return self.hub.tools

    def summary(self, tool_id: str) -> Summary:
        return self.summaries.get(tool_id) or Summary(state="off", label="Not running")

    def policy(self) -> str:
        s = load_settings()
        if s.gpu_policy in ("exclusive", "budget"):
            return s.gpu_policy
        total = self.hub.gpu.latest.total_mb
        return "budget" if total >= 20 * 1024 else "exclusive"

    def idle_minutes(self) -> tuple[float, float]:
        s = load_settings()
        auto_unload, auto_stop = idle_defaults(self.hub.gpu.latest.total_mb)
        unload = s.idle_unload_min if s.idle_unload_min else auto_unload
        stop = s.idle_stop_min if s.idle_stop_min else auto_stop
        return unload, stop

    def note_activity(self, tool_id: str) -> None:
        self.last_activity[tool_id] = time.time()

    def _record(self, tool_id: str | None, text: str, level: str = "info") -> None:
        self.recent_actions.append({"ts": time.time(), "tool": tool_id, "text": text})
        self.hub.bus.log(text, tool=tool_id, level=level, source="orchestrator")

    def describe(self) -> dict[str, Any]:
        unload_min, stop_min = self.idle_minutes()
        return {"owner": self.owner, "owner_since": self.owner_since, "policy": self.policy(),
                "focus": self.focus, "pending": list(self.pending), "idle_unload_min": unload_min,
                "idle_stop_min": stop_min, "recent_actions": list(self.recent_actions)[-12:]}

    # ------------------------------------------------------------------ status polling
    async def refresh(self, tool: "ManagedProcess") -> Summary:
        if not tool.running:
            summ = Summary(state="off", label="Not running")
        else:
            try:
                r = await self.hub.http.get(f"{tool.base_url}{tool.spec.status_path}", timeout=4.0)
                body = r.json() if r.status_code == 200 else {}
                extra: dict[str, Any] = {}
                for key, url in tool.spec.status_requests(tool.base_url, body if isinstance(body, dict) else {}):
                    try:
                        rr = await self.hub.http.get(url, timeout=4.0)
                        if rr.status_code == 200:
                            extra[key] = rr.json()
                    except Exception:
                        pass
                summ = tool.spec.parse_status(body if isinstance(body, dict) else {}, extra)
            except Exception as exc:
                summ = Summary(state="unknown", label="No answer from the tool", detail=str(exc)[:120])
        prev = self.summaries.get(tool.id)
        self.summaries[tool.id] = summ
        self.summary_ts[tool.id] = time.time()
        if summ.busy:
            self.note_activity(tool.id)
        if prev is not None and (prev.state != summ.state or prev.label != summ.label):
            self.hub.bus.publish({"type": "summary", "tool": tool.id, "summary": summ.to_dict()})
        return summ

    async def _poll_loop(self) -> None:
        while True:
            try:
                await asyncio.gather(*(self.refresh(t) for t in self.tools.values()))
                self.hub.bus.publish({"type": "state", "state": self.hub.snapshot()})
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(2.5)

    # ------------------------------------------------------------------ HTTP steps
    async def _call(self, step: Step) -> tuple[bool, str]:
        try:
            if step.fire_and_forget:
                try:
                    r = await self.hub.http.request(step.method, step.url, json=step.json, timeout=step.timeout)
                    return r.status_code < 400, f"HTTP {r.status_code}"
                except (httpx.ReadTimeout, httpx.PoolTimeout):
                    return True, "started"
            r = await self.hub.http.request(step.method, step.url, json=step.json, timeout=step.timeout)
            if r.status_code >= 400:
                try:
                    detail = r.json().get("detail")
                except Exception:
                    detail = r.text[:120]
                return False, f"HTTP {r.status_code}: {detail}"
            return True, "ok"
        except Exception as exc:
            return False, f"{type(exc).__name__}"

    async def unload_models(self, tool: "ManagedProcess", why: str = "") -> bool:
        if not tool.running:
            return True
        steps = tool.spec.unload_steps(tool.base_url, tool.cfg, tool.tool_dir)
        if not steps:
            return True
        ok_all = True
        for step in steps:
            ok, info = await self._call(step)
            if not ok and "HTTP 409" in info:
                ok_all = False   # a job is running; the caller waits and retries
            elif not ok and step.label.startswith("unload Chatterbox"):
                pass             # helper not running: fine
        self._record(tool.id, f"Released the GPU from {tool.spec.name}" + (f" ({why})" if why else ""))
        await self.refresh(tool)
        return ok_all

    async def on_started(self, tool: "ManagedProcess") -> None:
        """A studio that loads a model on its own at boot (Forge loads its last checkpoint) must not keep it
        while another studio holds the GPU: unload it straight away. Called right after a tool becomes ready."""
        await asyncio.sleep(2.0)
        if not tool.running or self._lock.locked():
            return
        summ = await self.refresh(tool)
        holder = self.owner
        if not holder or holder == tool.id or holder not in self.tools or not summ.loaded or summ.busy:
            return
        if not tool.spec.supports_unload or self.tools[holder].state != "running":
            return
        await self.unload_models(tool, f"{self.tools[holder].spec.name} holds the GPU")

    async def prepare(self, tool_id: str, reason: str = "") -> ClaimResult:
        tool = self.tools[tool_id]
        res = await self.claim(tool_id, reason or "warm-up", wait=False)
        if not res.ok:
            return res
        summ = self.summary(tool_id)
        if summ.loaded and summ.state in ("ready", "loading", "busy"):
            return res
        for step in tool.spec.prepare_steps(tool.base_url, tool.cfg, tool.tool_dir):
            ok, info = await self._call(step)
            if ok:
                res.actions.append(step.label)
        if res.actions:
            self._record(tool_id, f"Warming up {tool.spec.name}: {', '.join(res.actions)}")
        return res

    # ------------------------------------------------------------------ the claim
    async def claim(self, tool_id: str, reason: str = "", wait: bool = True) -> ClaimResult:
        tool = self.tools[tool_id]
        s = load_settings()
        entry = {"tool": tool_id, "reason": reason, "since": time.time(), "waiting_for": None}
        self.pending.append(entry)
        t0 = time.time()
        try:
            async with self._lock:
                self.note_activity(tool_id)
                if not await tool.ensure_running():
                    return ClaimResult(False, tool_id, warning=tool.error or f"{tool.spec.name} could not be started")

                # 1. Never interrupt a job that is running in another studio - wait for it.
                waited_msg = None
                deadline = t0 + max(1.0, s.claim_wait_max_min) * 60
                while True:
                    busy_others = [t for t in self.tools.values() if t.id != tool_id and t.running and self.summary(t.id).busy]
                    if not busy_others:
                        break
                    if not wait:
                        return ClaimResult(False, tool_id, warning=f"{busy_others[0].spec.name} is busy")
                    names = ", ".join(t.spec.name for t in busy_others)
                    entry["waiting_for"] = [t.id for t in busy_others]
                    if waited_msg != names:
                        waited_msg = names
                        self._record(tool_id, f"{tool.spec.name} is waiting for {names} to finish")
                        self.hub.bus.publish({"type": "waiting", "tool": tool_id, "for": [t.id for t in busy_others]})
                    if time.time() > deadline:
                        return ClaimResult(False, tool_id, warning=f"Gave up waiting for {names}", waited_s=time.time() - t0)
                    await asyncio.sleep(2.0)
                    for t in busy_others:
                        await self.refresh(t)
                entry["waiting_for"] = None

                gpu = await self.hub.gpu.refresh()
                actions: list[str] = []
                if not gpu.available:
                    self._set_owner(tool_id)
                    return ClaimResult(True, tool_id, actions, waited_s=time.time() - t0)

                total_gb = gpu.total_mb / 1024
                need_mb = int((tool.spec.vram_need_gb(total_gb) + s.vram_headroom_gb) * 1024)
                # The desktop, browsers and the driver always keep ~1 GB: never demand more than can exist.
                need_mb = min(need_mb, gpu.total_mb - 1200)

                async def enough() -> bool:
                    info = await self.hub.gpu.refresh()
                    return info.free_mb >= need_mb

                others = [t for t in self.tools.values() if t.id != tool_id and t.running]
                mine = self.summary(tool_id)
                if self.owner == tool_id and mine.loaded and await enough():
                    return ClaimResult(True, tool_id, actions, waited_s=time.time() - t0, free_mb=self.hub.gpu.latest.free_mb)

                # 2. Unload the other studios' models.
                policy = self.policy()
                loaded_others = [t for t in others if self.summary(t.id).loaded]
                if policy == "budget":
                    loaded_others.sort(key=lambda t: t.spec.vram_need_gb(total_gb), reverse=True)
                for t in loaded_others:
                    if policy == "budget" and await enough():
                        break
                    await self.unload_models(t, f"{tool.spec.name} needs the GPU")
                    actions.append(f"unloaded {t.spec.name}")
                if actions:
                    await self._settle()

                # 3. Still too full? Idle processes pin CUDA contexts - stop them, least recently used first.
                #    (Processes that never touch CUDA, like Lumen's backend with its engine off, are skipped.)
                if not await enough():
                    def holds_context(t: "ManagedProcess") -> bool:
                        return t.spec.idle_context_mb > 0 or self.summary(t.id).loaded
                    order = sorted((t for t in others if holds_context(t) and not t.external),
                                   key=lambda t: (bool(t.cfg.get("pinned")), self.last_activity.get(t.id, 0.0)))
                    for t in order:
                        if await enough():
                            break
                        await t.stop(f"{tool.spec.name} needs the VRAM")
                        actions.append(f"stopped {t.spec.name}")
                        await self._settle(6.0)

                # 4. A local Ollama (lyric writing in Music Studio) keeps models resident for minutes.
                if not await enough() and s.release_ollama:
                    if await self._release_ollama():
                        actions.append("released Ollama models")
                        await self._settle(6.0)

                info = self.hub.gpu.latest
                self._set_owner(tool_id)
                warning = ""
                if info.free_mb < need_mb:
                    warning = (f"Only {info.free_mb / 1024:.1f} GB of {total_gb:.0f} GB VRAM is free; "
                               f"{tool.spec.name} would like about {need_mb / 1024:.1f} GB. Other applications may be using the GPU.")
                summary_text = f"GPU handed to {tool.spec.name}" + (f" - {', '.join(actions)}" if actions else "")
                if reason:
                    summary_text += f" [{reason}]"
                self._record(tool_id, summary_text, "warn" if warning else "info")
                res = ClaimResult(True, tool_id, actions, warning, time.time() - t0, info.free_mb)
                self.hub.bus.publish({"type": "claim", **res.to_dict(), "name": tool.spec.name})
                return res
        finally:
            if entry in self.pending:
                self.pending.remove(entry)

    async def before_forward(self, tool_id: str, method: str, path: str) -> list[str]:
        """After a successful claim: whatever the tool needs queued ahead of the request itself."""
        tool = self.tools[tool_id]
        summ = await self.refresh(tool)
        done: list[str] = []
        for step in tool.spec.pre_forward_steps(tool.base_url, method, path, summ):
            ok, _info = await self._call(step)
            if ok:
                done.append(step.label)
        if done:
            self._record(tool_id, f"{tool.spec.name}: {', '.join(done)} ahead of the request")
        return done

    def _set_owner(self, tool_id: str) -> None:
        if self.owner != tool_id:
            self.owner, self.owner_since = tool_id, time.time()

    async def _settle(self, max_wait: float = 12.0) -> None:
        """Wait until nvidia-smi shows the memory figure has stopped moving (drivers free lazily)."""
        last: int | None = None
        stable = 0
        t0 = time.time()
        while time.time() - t0 < max_wait:
            await asyncio.sleep(0.6)
            info = await self.hub.gpu.refresh()
            if last is not None and abs(info.used_mb - last) < 48:
                stable += 1
            else:
                stable = 0
            last = info.used_mb
            if stable >= 2 and time.time() - t0 >= 1.8:
                break

    async def _release_ollama(self) -> bool:
        try:
            r = await self.hub.http.get(f"{OLLAMA_URL}/api/ps", timeout=1.5)
            models = [m.get("name") for m in (r.json().get("models") or []) if m.get("name")]
        except Exception:
            return False
        released = False
        for name in models:
            try:
                await self.hub.http.post(f"{OLLAMA_URL}/api/generate", json={"model": name, "keep_alive": 0}, timeout=10)
                released = True
            except Exception:
                pass
        if released:
            self._record(None, f"Asked Ollama to release {len(models)} model(s)")
        return released

    # ------------------------------------------------------------------ user-facing actions
    async def free_gpu(self, stop_processes: bool = False) -> list[str]:
        actions: list[str] = []
        async with self._lock:
            for t in self.tools.values():
                if not t.running:
                    continue
                if self.summary(t.id).busy:
                    continue
                if self.summary(t.id).loaded or t.id == "video":
                    await self.unload_models(t, "free GPU")
                    actions.append(f"unloaded {t.spec.name}")
                if stop_processes and not t.external:
                    await t.stop("free GPU")
                    actions.append(f"stopped {t.spec.name}")
            self.owner = None
            await self._settle(6.0)
        if actions:
            self.hub.bus.publish({"type": "claim", "ok": True, "tool": None, "name": "GPU", "actions": actions,
                                  "warning": "", "free_mb": self.hub.gpu.latest.free_mb})
        return actions

    async def set_focus(self, tool_id: str | None) -> None:
        self.focus, self.focus_ts = tool_id, time.time()
        if tool_id in self.tools and load_settings().prepare_on_switch:
            if self._prepare_task and not self._prepare_task.done():
                self._prepare_task.cancel()
            self._prepare_task = asyncio.create_task(self._prepare_later(tool_id, self.focus_ts))

    async def _prepare_later(self, tool_id: str, stamp: float) -> None:
        try:
            await asyncio.sleep(max(0.5, load_settings().prepare_delay_s))
        except asyncio.CancelledError:
            return
        if self.focus != tool_id or self.focus_ts != stamp:
            return
        tool = self.tools.get(tool_id)
        if not tool or not tool.enabled:
            return
        if any(self.summary(t.id).busy for t in self.tools.values() if t.running):
            return
        if self.owner == tool_id and self.summary(tool_id).loaded:
            return
        if self._lock.locked():
            return
        if tool.state == "starting":
            # The studio was just opened and is still booting: warm it up once it answers.
            if not await tool.ensure_running() or self.focus != tool_id or self.focus_ts != stamp:
                return
            await self.refresh(tool)
            if any(self.summary(t.id).busy for t in self.tools.values() if t.running):
                return
        await self.prepare(tool_id, "switched to " + tool.spec.name)

    # ------------------------------------------------------------------ idle policy
    async def _idle_loop(self) -> None:
        while True:
            await asyncio.sleep(20)
            try:
                await self._apply_idle()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    async def _apply_idle(self) -> None:
        unload_min, stop_min = self.idle_minutes()
        now = time.time()
        shell_open = self.hub.bus.connected > 0
        for t in list(self.tools.values()):
            if not t.running or t.state != "running":
                continue
            summ = self.summary(t.id)
            if summ.busy or self._lock.locked():
                continue
            last = self.last_activity.get(t.id) or t.started_at or now
            idle_min = (now - last) / 60
            if unload_min > 0 and summ.loaded and idle_min >= unload_min and t.spec.supports_unload:
                await self.unload_models(t, f"idle for {int(idle_min)} min")
                self.last_activity[t.id] = now - unload_min * 60  # keep the stop timer running
                continue
            if stop_min > 0 and idle_min >= stop_min and not t.cfg.get("pinned") and not t.external:
                if shell_open and self.focus == t.id:
                    continue     # the user is looking at it
                await t.stop(f"idle for {int(idle_min)} min")
                if self.owner == t.id:
                    self.owner = None
