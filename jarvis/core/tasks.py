"""Hintergrund-Tasks, die nicht vom Garbage Collector eingesammelt werden.

``asyncio.create_task`` hält nur eine schwache Referenz – ein Task ohne
gespeicherte Referenz kann mitten in der Arbeit verschwinden. ``spawn`` merkt
sich den Task bis zum Ende und protokolliert unerwartete Fehler.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

log = logging.getLogger(__name__)

_RUNNING: set[asyncio.Task[Any]] = set()


def _done(task: asyncio.Task[Any]) -> None:
    _RUNNING.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("Hintergrundaufgabe %s fehlgeschlagen", task.get_name(), exc_info=exc)


def spawn(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task[Any]:
    """Startet ``coro`` als Task und hält eine Referenz, bis er fertig ist."""
    task = asyncio.ensure_future(coro)
    if name:
        task.set_name(name)
    _RUNNING.add(task)
    task.add_done_callback(_done)
    return task
