"""Hintergrund-Tasks, die nicht vom Garbage Collector eingesammelt werden.

``asyncio.create_task`` hält nur eine schwache Referenz – ein Task ohne
gespeicherte Referenz kann mitten in der Arbeit verschwinden. ``spawn`` merkt
sich den Task bis zum Ende und protokolliert unerwartete Fehler.

``kill_process_tree`` beendet einen Kindprozess samt dessen Kindern.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Coroutine
from typing import Any

import psutil

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


def kill_process_tree(pid: int) -> None:
    """Beendet den eigenen Kindprozess ``pid`` und alle seine Nachkommen (hart).

    Unter Windows reißt das Beenden eines Prozesses seine Kinder nicht mit: Hinter
    ``claude.cmd`` (npm) liefe ``node.exe`` weiter, hinter PowerShell z. B. ein
    ``ping -t`` – und sie hielten obendrein die Ausgabe-Pipes offen.
    """
    try:
        parent = psutil.Process(pid)
        if parent.ppid() != os.getpid():  # PID inzwischen neu vergeben – nicht unser Prozess
            return
        children = parent.children(recursive=True)
    except psutil.Error:
        return
    for proc in (parent, *children):
        with contextlib.suppress(psutil.Error):
            proc.kill()
