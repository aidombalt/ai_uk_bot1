"""SSE-broadcaster для ленты активности.

Простая шина: подписчики (GUI-вкладки) держат asyncio.Queue, бот публикует
события — broadcast рассылает всем. На уровне процесса; для multi-process
нужен Redis pub/sub, но Sprint 6 один процесс.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from balt_dom_bot.log import get_logger

log = get_logger(__name__)


@dataclass(eq=False)
class _Subscriber:
    queue: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(maxsize=100))


class EventBus:
    def __init__(self) -> None:
        self._subs: set[_Subscriber] = set()

    def subscribe(self) -> _Subscriber:
        sub = _Subscriber()
        self._subs.add(sub)
        log.debug("eventbus.subscribed", subs=len(self._subs))
        return sub

    def unsubscribe(self, sub: _Subscriber) -> None:
        self._subs.discard(sub)
        log.debug("eventbus.unsubscribed", subs=len(self._subs))

    def publish(self, event_type: str, data: dict[str, Any]) -> None:
        if not self._subs:
            return
        msg = f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        dropped = 0
        for sub in list(self._subs):
            try:
                sub.queue.put_nowait(msg)
            except asyncio.QueueFull:
                dropped += 1
        if dropped:
            log.warning("eventbus.dropped", dropped=dropped)
