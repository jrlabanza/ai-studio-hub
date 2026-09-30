"""A tiny in-process event bus: SSE subscribers plus a ring buffer of recent events."""
from __future__ import annotations

import asyncio
import collections
import time
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.subscribers: set[asyncio.Queue] = set()
        self.recent: collections.deque[dict[str, Any]] = collections.deque(maxlen=300)
        self._seq = 0

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    @property
    def connected(self) -> int:
        return len(self.subscribers)

    def publish(self, event: dict[str, Any]) -> None:
        self._seq += 1
        event = {"seq": self._seq, "ts": time.time(), **event}
        if event.get("type") != "state":
            self.recent.append(event)
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def publish_threadsafe(self, event: dict[str, Any]) -> None:
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.publish, event)
        else:
            self.publish(event)

    # Convenience: a human readable line in the activity feed.
    def log(self, message: str, *, tool: str | None = None, level: str = "info", **extra: Any) -> None:
        self.publish({"type": "log", "level": level, "tool": tool, "message": message, **extra})

    def log_threadsafe(self, message: str, *, tool: str | None = None, level: str = "info", **extra: Any) -> None:
        self.publish_threadsafe({"type": "log", "level": level, "tool": tool, "message": message, **extra})
