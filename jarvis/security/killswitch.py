"""Not-Aus: stoppt sofort alle laufenden Antworten, Projekte und Selbstverbesserungen.

Auslösbar per Hotkey (Standard Strg+Alt+Umschalt+X), Tray-Menü, Dashboard,
Sprachbefehl ("Jarvis, Not-Aus") oder ``jarvis stop-all``. Solange der Not-Aus
aktiv ist, führt JARVIS keine Werkzeuge aus, bis du ihn wieder freigibst.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import Any

from jarvis.core.events import EventBus
from jarvis.core.tasks import spawn

log = logging.getLogger(__name__)


class KillSwitchEngaged(RuntimeError):
    """Wird geworfen, wenn eine Aktion bei aktivem Not-Aus starten soll."""


class KillSwitch:
    def __init__(self, events: EventBus) -> None:
        self._events = events
        self._engaged = threading.Event()
        self._lock = threading.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._callbacks: list[Callable[[], Any]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self.reason = ""

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def engaged(self) -> bool:
        return self._engaged.is_set()

    def check(self) -> None:
        if self.engaged:
            raise KillSwitchEngaged(f"Not-Aus ist aktiv ({self.reason})")

    def track(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        """Merkt sich eine Aufgabe, damit der Not-Aus sie abbrechen kann."""
        with self._lock:
            self._tasks.add(task)
        task.add_done_callback(self._forget)
        return task

    def _forget(self, task: asyncio.Task[Any]) -> None:
        with self._lock:
            self._tasks.discard(task)

    def on_engage(self, callback: Callable[[], Any]) -> None:
        """Zusätzliche Aufräumarbeit, z. B. Sprachausgabe stoppen, Prozesse beenden."""
        self._callbacks.append(callback)

    def engage(self, reason: str = "manuell", *, by: str = "user") -> None:
        """Threadsicher – darf aus Hotkey-, Tray- oder Web-Threads aufgerufen werden."""
        self._engaged.set()
        self.reason = reason
        log.warning("NOT-AUS ausgelöst (%s, durch %s)", reason, by)
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is not None and running is not loop:
            loop.call_soon_threadsafe(self._cancel_all)
        else:
            self._cancel_all()
        self._events.publish("killswitch.engaged", {"reason": reason, "by": by})

    def _cancel_all(self) -> None:
        with self._lock:
            tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for callback in self._callbacks:
            try:
                result = callback()
                if asyncio.iscoroutine(result):
                    spawn(result, name="killswitch-callback")
            except Exception:
                log.exception("Not-Aus-Callback fehlgeschlagen")

    def release(self, *, by: str = "user") -> None:
        self._engaged.clear()
        self.reason = ""
        log.warning("Not-Aus aufgehoben (durch %s)", by)
        self._events.publish("killswitch.released", {"by": by})
