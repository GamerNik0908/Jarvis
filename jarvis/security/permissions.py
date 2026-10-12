"""Berechtigungsstufen und Bestätigungen.

* ``auto`` – harmlos, läuft sofort (z. B. Websuche, Systeminfo, Programm öffnen)
* ``confirm`` – braucht deine Zustimmung (löschen, Systemänderung, E-Mail senden,
  PowerShell, Käufe …). Die Anfrage erscheint im Dashboard, als Benachrichtigung,
  im Terminal und – falls aktiv – per Sprache.
* ``forbidden`` – wird nie ausgeführt.

Antwortest du nicht innerhalb von ``security.confirm_timeout_s``, gilt die
Aktion als abgelehnt. Der Not-Aus lehnt alle offenen Anfragen sofort ab.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from jarvis.core.events import EventBus
from jarvis.core.timeutil import now_iso

log = logging.getLogger(__name__)


class Level(StrEnum):
    AUTO = "auto"
    CONFIRM = "confirm"
    FORBIDDEN = "forbidden"


LEVEL_RANK = {Level.AUTO: 0, Level.CONFIRM: 1, Level.FORBIDDEN: 2}


def stricter(a: Level, b: Level) -> Level:
    return a if LEVEL_RANK[a] >= LEVEL_RANK[b] else b


@dataclass
class PendingConfirmation:
    id: str
    tool: str
    summary: str
    details: dict[str, Any]
    session_id: str | None
    source: str
    created: str
    future: asyncio.Future[bool] = field(repr=False)
    loop: asyncio.AbstractEventLoop = field(repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool,
            "summary": self.summary,
            "details": self.details,
            "session_id": self.session_id,
            "source": self.source,
            "created": self.created,
        }


class ConfirmationBroker:
    """Sammelt offene Bestätigungsanfragen und wartet auf deine Antwort."""

    def __init__(self, events: EventBus, *, timeout_s: float = 600) -> None:
        self._events = events
        self._timeout_s = timeout_s
        self._pending: dict[str, PendingConfirmation] = {}
        self._lock = threading.Lock()
        #: Automatische Zustimmung (nur für Tests und bewusst freigegebene Abläufe).
        self.auto_answer: bool | None = None

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return [p.to_dict() for p in self._pending.values()]

    async def request(
        self,
        *,
        tool: str,
        summary: str,
        details: dict[str, Any] | None = None,
        session_id: str | None = None,
        source: str = "chat",
        timeout_s: float | None = None,
    ) -> tuple[bool, str]:
        """Fragt nach und liefert ``(zugestimmt, entscheidung)``."""
        if self.auto_answer is not None:
            return self.auto_answer, "approved" if self.auto_answer else "denied"
        loop = asyncio.get_running_loop()
        item = PendingConfirmation(
            id=uuid.uuid4().hex[:10],
            tool=tool,
            summary=summary,
            details=details or {},
            session_id=session_id,
            source=source,
            created=now_iso(),
            future=loop.create_future(),
            loop=loop,
        )
        with self._lock:
            self._pending[item.id] = item
        self._events.publish("confirmation.requested", item.to_dict(), session_id=session_id)
        # Wird die wartende Aufgabe abgebrochen (z. B. durch den Not-Aus), muss die
        # Anfrage trotzdem als erledigt gemeldet werden – sonst bleibt der Dialog offen.
        approved, decision = False, "cancelled"
        try:
            approved = await asyncio.wait_for(
                asyncio.shield(item.future), timeout_s or self._timeout_s
            )
            decision = "approved" if approved else "denied"
        except TimeoutError:
            approved, decision = False, "timeout"
        finally:
            with self._lock:
                self._pending.pop(item.id, None)
            self._events.publish(
                "confirmation.resolved",
                {"id": item.id, "approved": approved, "decision": decision},
                session_id=session_id,
            )
        return approved, decision

    def resolve(self, confirmation_id: str, approved: bool) -> bool:
        """Beantwortet eine Anfrage. Threadsicher (Tray, Sprache, Web)."""
        with self._lock:
            item = self._pending.get(confirmation_id)
        if item is None:
            return False

        def _set() -> None:
            if not item.future.done():
                item.future.set_result(approved)

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is item.loop:
            _set()
        else:
            item.loop.call_soon_threadsafe(_set)
        return True

    def latest(self) -> dict[str, Any] | None:
        with self._lock:
            if not self._pending:
                return None
            return list(self._pending.values())[-1].to_dict()

    def deny_all(self) -> int:
        with self._lock:
            ids = list(self._pending)
        for confirmation_id in ids:
            self.resolve(confirmation_id, False)
        return len(ids)


def describe_arguments(arguments: dict[str, Any], limit: int = 300) -> str:
    parts = []
    for key, value in arguments.items():
        text = str(value)
        if len(text) > 120:
            text = text[:117] + "…"
        parts.append(f"{key}={text}")
    summary = ", ".join(parts)
    return summary if len(summary) <= limit else summary[: limit - 1] + "…"


async def cancel_quietly(task: asyncio.Task[Any]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
