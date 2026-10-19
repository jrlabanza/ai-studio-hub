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
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
import psutil

from .config import load_settings
from .tools import Step, Summary

if TYPE_CHECKING:
    from .core import Hub
    from .process import ManagedProcess

OLLAMA_URL = "http://127.0.0.1:11434"
LEASE_GRACE_S = 8.0


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
    skipped: bool = False           # taken out of the queue by the user (or it timed out): the studio should not retry

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "tool": self.tool, "actions": self.actions, "warning": self.warning,
                "waited_s": round(self.waited_s, 1), "free_mb": self.free_mb, "skipped": self.skipped}


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
        # The GPU lease: the studio a claim was granted to keeps the GPU until its job has been seen running and has
        # finished - a status poll comes every 2.5 s, so without it the next claim could slip in before the first
        # studio looked busy and unload its model under its own job. A claim that never turns into a job (a quick
        # request) lets the lease lapse after LEASE_GRACE_S.
        self.lease: dict[str, Any] | None = None
        self.inflight: dict[str, int] = {}          # claimed requests still being answered, per studio
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
                "focus": self.focus, "pending": list(self.pending), "queue": self.queue(),
                "lease": dict(self.lease) if self.lease else None, "idle_unload_min": unload_min,
                "idle_stop_min": stop_min, "recent_actions": list(self.recent_actions)[-12:]}

    def lease_holder(self, tool_id: str) -> str | None:
        """Another studio whose granted job has not finished yet (or has not had time to show up), else None."""
        lz = self.lease
        if not lz or lz["tool"] == tool_id or lz["tool"] not in self.tools:
            return None
        holder = self.tools[lz["tool"]]
        if not holder.running:
            self.lease = None
            return None
        if self.summary(holder.id).busy or self.inflight.get(holder.id, 0) > 0:
            return holder.id
        if not lz.get("seen_busy") and time.time() - max(lz["granted"], lz.get("answered", 0.0)) < LEASE_GRACE_S:
            return holder.id
        self.lease = None                        # its job finished (or it never became one)
        return None

    def queue(self) -> list[dict[str, Any]]:
        """The hub's GPU queue, in order: the job holding the GPU, then every request waiting for its turn."""
        out: list[dict[str, Any]] = []
        running = next((t for t in self.tools.values() if t.running and self.summary(t.id).busy), None)
        holder = self.lease_holder("") or (running.id if running else None)     # a finished lease is dropped here
        if holder and holder in self.tools:
            job = self.summary(holder).job or {}
            out.append({"tool": holder, "name": self.tools[holder].spec.name, "status": "running",
                        "title": job.get("title") or "", "percent": job.get("percent"), "message": job.get("message") or ""})
        wait_max = load_settings().claim_wait_max_min
        for i, e in enumerate(self.pending):
            out.append({"id": e["id"], "tool": e["tool"],
                        "name": self.tools[e["tool"]].spec.name if e["tool"] in self.tools else e["tool"],
                        "status": "waiting", "position": i + 1, "since": e["since"], "reason": e.get("reason", ""),
                        "waiting_for": e.get("waiting_for") or [], "blocked": e.get("blocked", ""),
                        "need_vram_gb": e.get("need_vram_gb"), "need_ram_gb": e.get("need_ram_gb"),
                        "gives_up_at": e["since"] + wait_max * 60 if wait_max > 0 else None})
        return out

    def skip(self, entry_id: str) -> bool:
        """The user took a waiting request out of the queue: its claim fails at once (the studio gets an error)."""
        for e in self.pending:
            if e["id"] == entry_id:
                e["skip"] = True
                e["event"].set()
                self._record(e["tool"], f"{self.tools[e['tool']].spec.name}: a queued request was skipped from the hub", "warn")
                return True
        return False

    def holds_gpu(self, tool_id: str) -> bool:
        """The studio has the GPU for a job right now: one is running there, or the claim granted to it is still being
        answered (or was granted a moment ago and has not shown up as a job yet)."""
        t = self.tools.get(tool_id)
        if not t or not t.running:
            return False
        if self.summary(tool_id).busy:
            return True
        lz = self.lease
        if not lz or lz["tool"] != tool_id:
            return False
        return self.inflight.get(tool_id, 0) > 0 or (
            not lz.get("seen_busy") and time.time() - max(lz["granted"], lz.get("answered", 0.0)) < LEASE_GRACE_S)

    def inflight_begin(self, tool_id: str) -> None:
        self.inflight[tool_id] = self.inflight.get(tool_id, 0) + 1

    def inflight_end(self, tool_id: str) -> None:
        self.inflight[tool_id] = max(0, self.inflight.get(tool_id, 0) - 1)
        if self.lease and self.lease["tool"] == tool_id:
            self.lease["answered"] = time.time()
        self._queue_changed()

    def _queue_changed(self) -> None:
        self.hub.bus.publish({"type": "queue", "queue": self.queue()})

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
        jw = getattr(self.hub, "jobwatch", None)
        if jw is not None:
            jw.observe(tool, summ)          # which job runs, how it ended (notifications, the watchdog)
        if summ.busy:
            self.note_activity(tool.id)
            if self.lease and self.lease["tool"] == tool.id:
                self.lease["seen_busy"] = True
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
    async def claim(self, tool_id: str, reason: str = "", wait: bool = True, path: str = "") -> ClaimResult:
        tool = self.tools[tool_id]
        s = load_settings()
        t0 = time.time()
        # 0. The studio that holds the GPU right now never queues behind itself: a request that belongs to its running
        #    job (an engine restart after an OOM, a prompt rewrite, a model load) - or any request of it while nobody
        #    else is waiting - goes straight through. Queued, it would wait behind a request that is itself waiting for
        #    this studio's job to end, which never ends if the job needs the request (a deadlock).
        others_waiting = any(e["tool"] != tool_id for e in self.pending)
        if self.holds_gpu(tool_id) and (tool.spec.is_followup(path) or not others_waiting):
            self.note_activity(tool_id)
            if not (self.lease and self.lease["tool"] == tool_id):
                self._grant(ClaimResult(True, tool_id), reason)
            self.hub.bus.log(f"{tool.spec.name}: {reason or 'request'} went straight through - it holds the GPU",
                             tool=tool_id, source="orchestrator", level="debug")
            return ClaimResult(True, tool_id, ["follow-up of its own job"])
        total_gb = (self.hub.gpu.latest.total_mb or 0) / 1024
        entry: dict[str, Any] = {"id": uuid.uuid4().hex[:10], "tool": tool_id, "reason": reason, "since": t0,
                                 "waiting_for": None, "blocked": "", "skip": False, "event": asyncio.Event(),
                                 "need_vram_gb": round(tool.spec.vram_need_gb(total_gb or 8), 1),
                                 "need_ram_gb": float(getattr(tool.spec, "ram_need_gb", 0) or 0) or None}
        self.pending.append(entry)
        if self._lock.locked():
            entry["blocked"] = "behind the request ahead of it"
        self._queue_changed()
        try:
            # Wait for the lock (FIFO across studios) while the user can still skip the request or it times out.
            if not await self._acquire(entry, wait):
                late = time.time() > self._deadline(entry)
                why = ("Skipped in the hub's GPU queue" if entry["skip"] else
                       "Gave up waiting in the hub's GPU queue" if late else "The GPU is busy")
                return ClaimResult(False, tool_id, warning=why, waited_s=time.time() - t0, skipped=entry["skip"] or late)
            try:
                return await self._claim_locked(tool, s, entry, reason, wait, t0)
            finally:
                self._lock.release()
        finally:
            if entry in self.pending:
                self.pending.remove(entry)
            self._queue_changed()

    @staticmethod
    def _deadline(entry: dict[str, Any]) -> float:
        """When a waiting request gives up - read live, so changing the setting applies to requests already waiting."""
        m = load_settings().claim_wait_max_min
        return entry["since"] + m * 60 if m > 0 else float("inf")

    async def _acquire(self, entry: dict[str, Any], wait: bool) -> bool:
        """Take the claim lock in arrival order; False when skipped, timed out, or (wait=False) when it is taken.
        The single acquire() call keeps the request's place in line; if this coroutine is abandoned in any way, a lock
        it was granted meanwhile is handed straight back, so a cancelled claim can never leave the queue locked."""
        if not wait and self._lock.locked():
            return False
        acq = asyncio.ensure_future(self._lock.acquire())
        stop = asyncio.ensure_future(entry["event"].wait())
        got = False
        try:
            while True:
                left = self._deadline(entry) - time.time()
                done, _ = await asyncio.wait({acq, stop}, timeout=min(30.0, max(0.1, left)),
                                             return_when=asyncio.FIRST_COMPLETED)
                if acq in done and not entry["skip"]:
                    got = True
                    return True
                if acq in done or entry["skip"] or time.time() > self._deadline(entry):
                    return False
        finally:
            stop.cancel()
            if not got:
                def give_back(f: asyncio.Future) -> None:
                    if not f.cancelled() and f.exception() is None:
                        self._lock.release()
                if acq.done():
                    give_back(acq)
                else:
                    acq.add_done_callback(give_back)
                    acq.cancel()

    async def _claim_locked(self, tool: "ManagedProcess", s: Any, entry: dict[str, Any], reason: str, wait: bool,
                            t0: float) -> ClaimResult:
        tool_id = tool.id
        entry["blocked"] = ""
        self._queue_changed()
        self.note_activity(tool_id)
        if not await tool.ensure_running():
            return ClaimResult(False, tool_id, warning=tool.error or f"{tool.spec.name} could not be started")

        # 1. Never interrupt a job that is running in another studio - wait for it. Requests line up behind
        #    the lock in arrival order (the hub's GPU queue); the studio granted last keeps the GPU until its
        #    job has finished (the lease), so a model is only ever switched between jobs. Only on a card big
        #    enough to share ("budget") may a job start next to a running one - and only when its VRAM and its
        #    system RAM need both fit in what is free right now, measured, not assumed.
        waited_msg = None
        while True:
            busy_others = [t for t in self.tools.values() if t.id != tool_id and t.running and self.summary(t.id).busy]
            holder = self.lease_holder(tool_id)
            if holder and all(t.id != holder for t in busy_others):
                busy_others.append(self.tools[holder])
            if not busy_others:
                break
            fits, why = await self._fits_alongside(tool, s)
            if fits:
                self._record(tool_id, f"{tool.spec.name} runs next to {', '.join(t.spec.name for t in busy_others)} - {why}")
                break
            if not wait:
                return ClaimResult(False, tool_id, warning=f"{busy_others[0].spec.name} is busy")
            names = ", ".join(t.spec.name for t in busy_others)
            entry["waiting_for"] = [t.id for t in busy_others]
            entry["blocked"] = f"waiting for {names} to finish" + (f" ({why})" if why else "")
            if waited_msg != names:
                waited_msg = names
                self._record(tool_id, f"{tool.spec.name} is waiting for {names} to finish")
                self.hub.bus.publish({"type": "waiting", "tool": tool_id, "for": [t.id for t in busy_others]})
                self._queue_changed()
            if entry["skip"]:
                return ClaimResult(False, tool_id, warning="Skipped in the hub's GPU queue", waited_s=time.time() - t0, skipped=True)
            if time.time() > self._deadline(entry):
                return ClaimResult(False, tool_id, warning=f"Gave up waiting for {names}", waited_s=time.time() - t0, skipped=True)
            try:
                await asyncio.wait_for(entry["event"].wait(), 1.0)
            except asyncio.TimeoutError:
                pass
            for t in busy_others:
                await self.refresh(t)
        entry["waiting_for"] = None
        entry["blocked"] = "getting the GPU ready"
        self._queue_changed()

        gpu = await self.hub.gpu.refresh()
        actions: list[str] = []
        if not gpu.available:
            self._set_owner(tool_id)
            return self._grant(ClaimResult(True, tool_id, actions, waited_s=time.time() - t0), reason)

        total_gb = gpu.total_mb / 1024
        need_mb = int((tool.spec.vram_need_gb(total_gb) + s.vram_headroom_gb) * 1024)
        # The desktop, browsers and the driver always keep ~1 GB: never demand more than can exist.
        need_mb = min(need_mb, gpu.total_mb - 1200)

        # What each running studio holds right now (per process, from nvidia-smi). The memory the
        # claiming studio already holds - its own loaded model - counts towards what it needs, so a
        # second Generate does not read as "the card is full" and start stopping the other studios.
        usage = await self._usage_by_tool()
        own_mb = usage.get(tool_id, 0)

        async def enough() -> bool:
            info = await self.hub.gpu.refresh()
            return info.free_mb + own_mb >= need_mb

        # A studio still running a job (only possible when sharing a big card) is never unloaded or stopped.
        others = [t for t in self.tools.values() if t.id != tool_id and t.running and not self.summary(t.id).busy]
        mine = self.summary(tool_id)
        if self.owner == tool_id and mine.loaded and await enough():
            return self._grant(ClaimResult(True, tool_id, actions, waited_s=time.time() - t0, free_mb=self.hub.gpu.latest.free_mb), reason)

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
        #    Only worth it when what they hold would actually close the gap: stopping two idle
        #    containers to win 300 MB against a 3 GB shortfall just costs a minute and helps nobody.
        if not await enough():
            usage = await self._usage_by_tool()
            own_mb = usage.get(tool_id, own_mb)
            def holds_context(t: "ManagedProcess") -> bool:
                return usage.get(t.id, 0) > 0 or t.spec.idle_context_mb > 0 or self.summary(t.id).loaded
            order = sorted((t for t in others if holds_context(t) and not t.external),
                           key=lambda t: (bool(t.cfg.get("pinned")), self.last_activity.get(t.id, 0.0)))
            reclaimable = sum(usage.get(t.id, t.spec.idle_context_mb) for t in order)
            shortfall = need_mb - (self.hub.gpu.latest.free_mb + own_mb)
            if reclaimable >= shortfall:
                for t in order:
                    if await enough():
                        break
                    await t.stop(f"{tool.spec.name} needs the VRAM")
                    t.held_until = time.time() + 10 * 60
                    actions.append(f"stopped {t.spec.name}")
                    await self._settle(6.0)
            else:
                self._record(tool_id, f"{tool.spec.name} would like {shortfall / 1024:.1f} GB more than is free; "
                             f"stopping the other studios would only give back {reclaimable / 1024:.1f} GB, so they stay up", "warn")

        # 3b. System RAM: an idle studio that was only unloaded keeps its process - and often its model,
        #     offloaded to RAM. A heavy job (MiniMax H3) then runs out of RAM and the kernel kills it, so
        #     idle studios are stopped, least recently used first, until the job's RAM need fits.
        need_ram = float(getattr(tool.spec, "ram_need_gb", 0) or 0)
        if need_ram:
            avail, own = await self._ram_for(tool_id)
            if avail + own < need_ram:
                idle = sorted((t for t in others if t.running and not t.external and not self.summary(t.id).busy
                               and not t.cfg.get("pinned")), key=lambda t: self.last_activity.get(t.id, 0.0))
                for t in idle:
                    await t.stop(f"{tool.spec.name} needs the RAM")
                    t.held_until = time.time() + 10 * 60
                    actions.append(f"stopped {t.spec.name} for RAM")
                    await asyncio.sleep(3.0)
                    avail, own = await self._ram_for(tool_id)
                    if avail + own >= need_ram:
                        break
                if avail + own < need_ram:
                    self._record(tool_id, f"{tool.spec.name} would like about {need_ram:.0f} GB of RAM; "
                                 f"{avail + own:.1f} GB is available - close other programs if the job fails", "warn")

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
        res = self._grant(ClaimResult(True, tool_id, actions, warning, time.time() - t0, info.free_mb), reason)
        self.hub.bus.publish({"type": "claim", **res.to_dict(), "name": tool.spec.name})
        return res

    async def _fits_alongside(self, tool: "ManagedProcess", s: Any) -> tuple[bool, str]:
        """(may this claim start while another studio's job runs, why). Never with the "exclusive" policy (cards under
        20 GB): one job at a time. With "budget": only when the free VRAM and the available system RAM - live figures -
        both cover this studio's need."""
        if self.policy() != "budget":
            return False, ""
        gpu = await self.hub.gpu.refresh()
        if not gpu.available:
            return False, ""
        usage = await self._usage_by_tool()
        need_mb = min(int((tool.spec.vram_need_gb(gpu.total_mb / 1024) + s.vram_headroom_gb) * 1024), gpu.total_mb - 1200)
        have_mb = gpu.free_mb + usage.get(tool.id, 0)
        if have_mb < need_mb:
            return False, f"needs {need_mb / 1024:.1f} GB VRAM, {have_mb / 1024:.1f} GB free"
        need_ram = float(getattr(tool.spec, "ram_need_gb", 0) or 0)
        if need_ram:
            avail, own = await self._ram_for(tool.id)
            if avail + own < need_ram:
                return False, f"needs {need_ram:.0f} GB RAM, {avail + own:.1f} GB available"
        return True, f"{have_mb / 1024:.1f} GB VRAM free covers its {need_mb / 1024:.1f} GB"

    def _grant(self, res: ClaimResult, reason: str) -> ClaimResult:
        """The GPU goes to res.tool: it holds the lease until its job has run (see lease_holder)."""
        self.lease = {"tool": res.tool, "granted": time.time(), "seen_busy": False, "reason": reason}
        return res

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

    async def _ram_for(self, tool_id: str) -> tuple[float, float]:
        """(GB of RAM available, GB already held by this studio's own processes - that counts towards its need)."""
        t = self.tools[tool_id]

        def calc() -> tuple[float, float]:
            own = 0
            if t.running:
                for p in psutil.process_iter(["pid", "memory_info"]):
                    mi = p.info.get("memory_info")
                    if mi and mi.rss > 512 * 2**20:
                        try:
                            if t.owns_pid(p.info["pid"]):
                                own += mi.rss
                        except Exception:
                            pass
            return psutil.virtual_memory().available / 2**30, own / 2**30
        return await asyncio.get_running_loop().run_in_executor(None, calc)

    async def _usage_by_tool(self) -> dict[str, int]:
        """VRAM in MB held by each running studio, from nvidia-smi's per-process list."""
        info = await self.hub.gpu.refresh(with_processes=True)
        out: dict[str, int] = {}
        loop = asyncio.get_running_loop()
        for p in info.processes:
            if not p.get("used_mb"):
                continue
            for t in self.tools.values():
                if t.running and await loop.run_in_executor(None, t.owns_pid, p["pid"]):
                    out[t.id] = out.get(t.id, 0) + int(p["used_mb"])
                    break
        return out

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
            holder = self.lease_holder("")             # a studio granted a job a moment ago keeps its model
            for t in self.tools.values():
                if not t.running:
                    continue
                if self.summary(t.id).busy or t.id == holder:
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
        if tool_id in self.tools:
            self.tools[tool_id].held_until = 0.0     # the user came back to it: it may start again
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
            if getattr(summ, "keep_alive", False):
                # downloading models (or similar GPU-free work): it is not idle, keep the timer fresh
                self.last_activity[t.id] = now
                continue
            # A restart counts as activity: never judge a studio by what it did before it was last stopped.
            last = max(self.last_activity.get(t.id, 0.0), t.started_at) or now
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
