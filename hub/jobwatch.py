"""Which job is running in each studio, how it ended, and the notifications for long ones.

The orchestrator polls every studio every 2.5 s (status_path); each poll is handed to observe(). A job is known by
the id the studio reports for its running job (Video Studio's queue.current_local, Image Studio's active job, ...),
or by its kind + title where the studio has no ids (Forge). When that id changes or the studio stops being busy,
the previous job is over: its result is read from the studio's own job API (ToolSpec.job_status_path) - succeeded,
failed or cancelled - and a job that ran longer than notify_min_s is announced:

* an SSE "job" event, which the shell turns into a toast and (when allowed) a browser notification;
* optionally a POST to an ntfy.sh-style push URL of the user's own. It carries only the studio, the status, the
  kind of job and how long it took - never the prompt, an error text or a file, unless notify_push_details is on.

The same record keeps the time the job's progress last moved (percent / stage / step), which the watchdog reads.
"""
from __future__ import annotations

import asyncio
import collections
import time
from typing import TYPE_CHECKING, Any

from .config import load_settings

if TYPE_CHECKING:
    from .core import Hub
    from .process import ManagedProcess
    from .tools import Summary

KIND_LABELS = {
    "t2v": "text to video", "i2v": "image to video", "flf2v": "first + last frame video", "swap": "character swap",
    "storyboard": "storyboard", "ref2v": "reference video", "extend": "extend", "edit": "video edit",
    "motion": "motion transfer", "keyframe": "keyframe animation", "facerefine": "face refine", "sequence": "timeline export",
    "subtitles": "subtitles", "reframe": "reframe", "grade": "colour grade", "generate": "generation",
    "song": "song", "load_model": "model load", "enhance": "prompt rewrite",
}
MODEL_LABELS = {"h3": "MiniMax H3", "ltx25": "LTX-2.5"}
STATUS_WORDS = {"succeeded": "finished", "failed": "failed", "cancelled": "was cancelled",
                "interrupted": "was interrupted", "finished": "finished"}


def _sig(job: dict[str, Any]) -> tuple:
    """What counts as progress: any change of the percentage, the stage line or the sampling step."""
    pct = job.get("percent")
    return (job.get("id"), round(float(pct), 1) if isinstance(pct, (int, float)) else None,
            str(job.get("message") or ""), job.get("step"))


def kind_label(tool_name: str, kind: str, model: str | None = None) -> str:
    k = KIND_LABELS.get(str(kind or ""), str(kind or "job").replace("_", " "))
    m = MODEL_LABELS.get(str(model or ""), "")
    return f"{m} {k}".strip() if m else k


class JobWatch:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.active: dict[str, dict[str, Any]] = {}
        self.recent: collections.deque[dict[str, Any]] = collections.deque(maxlen=40)

    # ------------------------------------------------------------------ tracking
    def observe(self, tool: "ManagedProcess", summ: "Summary") -> None:
        if summ.state == "unknown":
            return              # no answer this time: says nothing about the job (the watchdog judges silence)
        job = summ.job if summ.busy and summ.job else None
        key = None
        if job and job.get("title") != "Queued":
            key = str(job.get("id") or f"{job.get('kind')}:{job.get('title')}")
        now = time.time()
        prev = self.active.get(tool.id)
        if prev and prev["key"] != key:
            del self.active[tool.id]
            try:
                asyncio.get_running_loop().create_task(self._finished(tool, prev))
            except RuntimeError:
                pass
        if not key:
            return
        cur = self.active.get(tool.id)
        if not cur:
            started = job.get("started")
            started = float(started) if isinstance(started, (int, float)) and 0 < started <= now else now
            self.active[tool.id] = {"key": key, "id": job.get("id"), "kind": job.get("kind") or "", "title": job.get("title") or "",
                                    "model": job.get("model"), "first_seen": now, "started": started,
                                    "sig": _sig(job), "moved": now, "message": job.get("message") or ""}
            return
        s = _sig(job)
        if s != cur["sig"]:
            cur["sig"], cur["moved"] = s, now
        cur["title"] = job.get("title") or cur["title"]
        cur["message"] = job.get("message") or cur["message"]

    def forget(self, tool_id: str) -> None:
        self.active.pop(tool_id, None)

    async def _result(self, tool: "ManagedProcess", job_id: Any) -> dict[str, Any]:
        path = tool.spec.job_status_path
        if not path or not job_id or not tool.running:
            return {}
        try:
            r = await self.hub.http.get(f"{tool.base_url}{path.format(id=job_id)}", timeout=5.0)
            if r.status_code == 200 and isinstance(r.json(), dict):
                return tool.spec.job_result(r.json())
        except Exception:
            pass
        return {}

    async def _finished(self, tool: "ManagedProcess", job: dict[str, Any]) -> None:
        now = time.time()
        res = await self._result(tool, job.get("id"))
        status = res.get("status") or ("interrupted" if not tool.running else "finished")
        started, finished = res.get("started"), res.get("finished")
        if isinstance(started, (int, float)) and isinstance(finished, (int, float)) and 0 < started <= finished:
            seconds = finished - started
        else:
            seconds = now - job["started"]
        s = load_settings()
        rec = {"tool": tool.id, "name": tool.spec.name, "key": job["key"], "status": status,
               "label": kind_label(tool.spec.name, res.get("kind") or job.get("kind"), job.get("model")),
               "title": job.get("title") or "", "seconds": round(seconds), "ended": now,
               "notify": seconds >= s.notify_min_s, "error": res.get("error") or ""}
        self.recent.append(rec)
        if not rec["notify"]:
            return
        mins = f"{seconds / 60:.0f} min" if seconds >= 90 else f"{seconds:.0f} s"
        self.hub.bus.log(f"{tool.spec.name}: {rec['label']} {STATUS_WORDS.get(status, status)} after {mins}",
                         tool=tool.id, level="warn" if status in ("failed", "interrupted") else "info", source="jobs")
        self.hub.bus.publish({"type": "job", **rec})
        if s.notify_push_url:
            await self.push(rec, s.notify_push_url, s.notify_push_details)

    # ------------------------------------------------------------------ push
    @staticmethod
    def push_message(rec: dict[str, Any], details: bool) -> tuple[str, str]:
        """(title, body) of a push. Without details: studio, kind, status and duration - nothing the user typed."""
        seconds = rec.get("seconds") or 0
        mins = f"{seconds / 60:.0f} min" if seconds >= 90 else f"{seconds:.0f} s"
        title = f"{rec['name']}: {rec['label']} {STATUS_WORDS.get(rec['status'], rec['status'])}"
        body = f"{rec['status']} after {mins}"
        if details:
            if rec.get("title"):
                body += f"\n{rec['title']}"
            if rec.get("error"):
                body += f"\n{rec['error']}"
        return title, body

    async def push(self, rec: dict[str, Any], url: str, details: bool) -> tuple[bool, str]:
        if not url.lower().startswith(("http://", "https://")):
            return False, "the push URL must start with http:// or https://"
        title, body = self.push_message(rec, details)
        bad = rec["status"] in ("failed", "interrupted")
        headers = {"Title": title.encode("ascii", "replace").decode("ascii"),     # HTTP headers are latin-1
                   "Tags": "x" if bad else "white_check_mark", "Priority": "high" if bad else "default",
                   "Content-Type": "text/plain; charset=utf-8"}
        try:
            r = await self.hub.http.post(url, content=body.encode("utf-8"), headers=headers, timeout=10.0)
            ok = r.status_code < 400
            if not ok:
                self.hub.bus.log(f"Push notification failed: HTTP {r.status_code}", level="warn", source="jobs")
            return ok, f"HTTP {r.status_code}"
        except Exception as exc:
            self.hub.bus.log(f"Push notification failed: {type(exc).__name__}", level="warn", source="jobs")
            return False, type(exc).__name__
