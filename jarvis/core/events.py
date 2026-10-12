"""Ereignisbus: verteilt Live-Ereignisse an Dashboard, Tray, Terminal und Sprache.

Beispiele: ``chat.delta`` (Text-Häppchen), ``tool.started``, ``image.new``,
``confirmation.requested``, ``improvement.log``, ``notification``.
``publish`` ist threadsicher – auch Tray-Icon und Sprach-Thread dürfen senden.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

_ids = itertools.count(1)


@dataclass(frozen=True)
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    ts: float = field(default_factory=time.time)
    id: int = field(default_factory=lambda: next(_ids))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "data": self.data,
            "session_id": self.session_id,
            "ts": self.ts,
        }


class Subscription:
    def __init__(
        self,
        bus: EventBus,
        *,
        session_id: str | None,
        types: frozenset[str] | None,
        prefixes: tuple[str, ...],
    ) -> None:
        self._bus = bus
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=1000)
        self.session_id = session_id
        self.types = types
        self.prefixes = prefixes

    def matches(self, event: Event) -> bool:
        if self.session_id is not None and event.session_id not in (None, self.session_id):
            return False
        if self.types is None and not self.prefixes:
            return True
        if self.types is not None and event.type in self.types:
            return True
        return any(event.type.startswith(p) for p in self.prefixes)

    def __aiter__(self) -> AsyncIterator[Event]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[Event]:
        while True:
            yield await self.queue.get()

    async def get(self, limit_s: float | None = None) -> Event | None:
        try:
            return await asyncio.wait_for(self.queue.get(), limit_s)
        except TimeoutError:
            return None

    def close(self) -> None:
        self._bus._remove(self)

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class EventBus:
    def __init__(self, history: int = 300) -> None:
        self._subscribers: list[Subscription] = []
        self._history: deque[Event] = deque(maxlen=history)
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(
        self,
        *,
        session_id: str | None = None,
        types: Iterable[str] | None = None,
        prefixes: Iterable[str] = (),
    ) -> Subscription:
        sub = Subscription(
            self,
            session_id=session_id,
            types=frozenset(types) if types is not None else None,
            prefixes=tuple(prefixes),
        )
        with self._lock:
            self._subscribers.append(sub)
        return sub

    def _remove(self, sub: Subscription) -> None:
        with self._lock, contextlib.suppress(ValueError):
            self._subscribers.remove(sub)

    def publish(
        self, type: str, data: dict[str, Any] | None = None, *, session_id: str | None = None
    ) -> Event:
        event = Event(type=type, data=dict(data or {}), session_id=session_id)
        with self._lock:
            self._history.append(event)
            targets = [s for s in self._subscribers if s.matches(event)]
        if not targets:
            return event
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        for sub in targets:
            if loop is not None and running is not loop:
                # Beim Herunterfahren ist die Schleife evtl. schon zu – dann nur verwerfen.
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(_put, sub.queue, event)
            else:
                _put(sub.queue, event)
        return event

    def recent(self, limit: int = 50, *, prefix: str = "") -> list[Event]:
        with self._lock:
            items = [e for e in self._history if e.type.startswith(prefix)]
        return items[-limit:] if limit > 0 else []


def _put(queue: asyncio.Queue[Event], event: Event) -> None:
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        log.debug("Ereignis-Warteschlange voll – %s verworfen", event.type)
