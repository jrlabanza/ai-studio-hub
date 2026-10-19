"""Health watchdog: restart a studio that is hung, and only one that is hung.

Two signs, checked every 30 s (Settings: watchdog_enabled, watchdog_stall_min, watchdog_health_min, watchdog_dry_run):

* **A stalled job.** The studio reports a running job whose progress, stage line and sampling step (hub/jobwatch.py)
  have not changed for watchdog_stall_min minutes (default 15). Only studios whose progress ticks at least every few
  minutes are judged (ToolSpec.stall_watch: Video Studio - an H3 step is ~20 s on 8 GB and a storyboard ~20 min in
  all, but every step moves the counter - Image Studio, Forge). Before acting it looks for any other sign of life
  and backs off when it finds one: the GPU still busy in the last two minutes, or Video Studio's engine log still
  growing. Progress that is moving never triggers a restart.
* **No answer.** The studio's container (or process) is up, but its status endpoint has not answered at all for
  watchdog_health_min minutes (default 5) - not merely slowly: every poll in that window failed.

The action is the hub's ordinary restart (the same as POST /api/tools/<id>/restart), at most once per studio per
30 minutes; it is logged in the activity feed and shown as a note on the studio's card. Dry run only logs it.
"""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from . import docker as dk
from .config import load_settings

if TYPE_CHECKING:
    from .core import Hub
    from .process import ManagedProcess

INTERVAL_S = 30.0
COOLDOWN_S = 30 * 60
GPU_ALIVE_UTIL = 30          # % utilisation in the last two minutes that counts as "still computing"


class Watchdog:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.no_answer: dict[str, float] = {}       # tool -> first failed poll of the current silence
        self.log_tail: dict[str, tuple[str, list[str]]] = {}   # tool -> (job key, last engine log lines)
        self.actions: dict[str, dict[str, Any]] = {}            # tool -> the last thing the watchdog did there
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="watchdog")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(INTERVAL_S)
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the watchdog itself must never die
                self.hub.bus.log(f"Watchdog check failed: {type(exc).__name__}: {exc}", level="debug", source="watchdog")

    def describe(self) -> dict[str, Any]:
        """The last action per studio, for the shell (cards show it for six hours)."""
        cutoff = time.time() - 6 * 3600
        return {tid: a for tid, a in self.actions.items() if a["ts"] >= cutoff}

    # ------------------------------------------------------------------ the checks
    def _process_up(self, t: "ManagedProcess") -> bool:
        if t.is_docker:
            return dk.container_state(t.spec.container_name()) == "running"
        return bool(t.proc and t.proc.poll() is None)

    async def check(self, now: float | None = None) -> list[dict[str, Any]]:
        s = load_settings()
        now = now or time.time()
        done: list[dict[str, Any]] = []
        if not s.watchdog_enabled:
            self.no_answer.clear()
            return done
        orch, jw = self.hub.orchestrator, self.hub.jobwatch
        for t in list(self.hub.tools.values()):
            if t.state != "running":
                self.no_answer.pop(t.id, None)
                continue
            summ = orch.summary(t.id)
            # 1. silence: every poll since `since` failed, the container is still up, and it is past its start-up time
            if summ.state == "unknown":
                since = self.no_answer.setdefault(t.id, orch.summary_ts.get(t.id, now))
                quiet = now - since
                if quiet >= s.watchdog_health_min * 60 and now - (t.started_at or now) >= t.spec.startup_timeout \
                        and await asyncio.to_thread(self._process_up, t):
                    gpu = self._gpu_alive(now)
                    if gpu:              # a backend too busy to answer while its engine renders: not hung
                        self.no_answer[t.id] = now
                        self.hub.bus.log(f"Watchdog: {t.spec.name} has not answered for {quiet / 60:.0f} min, but {gpu} - "
                                         "leaving it alone", tool=t.id, level="info", source="watchdog")
                        continue
                    a = await self._act(t, f"its status endpoint has not answered for {quiet / 60:.0f} min while its "
                                           f"{'container' if t.is_docker else 'process'} is up", s, now)
                    if a:
                        done.append(a)
                continue
            self.no_answer.pop(t.id, None)
            # 2. a stalled job
            job = jw.active.get(t.id)
            if not t.spec.stall_watch or not summ.busy or not job:
                continue
            still = now - job["moved"]
            if still < s.watchdog_stall_min * 60:
                continue
            alive = await self._sign_of_life(t, job, now)
            if alive:
                job["moved"] = now                       # it is working, just without ticking the counter
                self.hub.bus.log(f"Watchdog: {t.spec.name}'s job has shown no progress for {still / 60:.0f} min, "
                                 f"but {alive} - leaving it alone", tool=t.id, level="info", source="watchdog")
                continue
            where = job.get("message") or "the same step"
            a = await self._act(t, f"its job has not moved for {still / 60:.0f} min (stuck at \"{where}\")", s, now)
            if a:
                done.append(a)
        return done

    def _gpu_alive(self, now: float) -> str:
        recent = [util for ts, _used, util in list(self.hub.gpu.history) if now - ts <= 120]
        if recent and max(recent) >= GPU_ALIVE_UTIL:
            return f"the GPU was {max(recent)}% busy in the last two minutes"
        return ""

    async def _sign_of_life(self, t: "ManagedProcess", job: dict[str, Any], now: float) -> str:
        """Why the job is still alive although its counter stands still ("" = no sign at all)."""
        gpu = self._gpu_alive(now)
        if gpu:
            return gpu
        if t.id == "video":
            try:
                r = await self.hub.http.get(f"{t.base_url}/api/engine/logs?lines=12", timeout=5.0)
                lines = [str(x) for x in (r.json().get("lines") or [])] if r.status_code == 200 else []
            except Exception:
                lines = []
            prev = self.log_tail.get(t.id)
            self.log_tail[t.id] = (job["key"], lines)
            if prev is None or prev[0] != job["key"]:
                return "its engine log is being checked first"     # nothing to compare with yet: look again next round
            if lines and lines != prev[1]:
                return "its engine log is still growing"
        return ""

    async def _act(self, t: "ManagedProcess", reason: str, s: Any, now: float) -> dict[str, Any] | None:
        last = self.actions.get(t.id)
        if last and now - last["ts"] < COOLDOWN_S:
            return None                                   # it already acted on this studio recently: do not loop
        dry = bool(s.watchdog_dry_run)
        a = {"ts": now, "tool": t.id, "name": t.spec.name, "reason": reason, "dry_run": dry,
             "action": "would restart" if dry else "restarted"}
        self.actions[t.id] = a
        msg = (f"Watchdog (dry run): would restart {t.spec.name} - {reason}" if dry
               else f"Watchdog restarted {t.spec.name} - {reason}")
        self.hub.bus.log(msg, tool=t.id, level="warn", source="watchdog")
        self.hub.bus.publish({"type": "watchdog", **a})
        if not dry:
            orch = self.hub.orchestrator
            if orch.lease and orch.lease.get("tool") == t.id:
                orch.lease = None                         # its job is gone: the queue moves on
            if orch.owner == t.id:
                orch.owner = None
            self.no_answer.pop(t.id, None)
            asyncio.create_task(t.restart())              # the hub's own restart path (as /api/tools/<id>/restart)
        return a
