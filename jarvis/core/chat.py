"""Chat-Sitzungen für Dashboard, Chat-Fenster und Tray: startet Agent-Läufe im Hintergrund.

Die Antwort wird als Ereignisse (``chat.delta``, ``tool.*``, ``chat.final``)
über den Ereignisbus gestreamt; das Dashboard empfängt sie per WebSocket.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from jarvis.core.types import TaskKind

if TYPE_CHECKING:
    from jarvis.core.app import JarvisApp

log = logging.getLogger(__name__)


class ChatService:
    def __init__(self, app: JarvisApp) -> None:
        self.app = app
        self._running: dict[str, asyncio.Task[None]] = {}

    def busy(self, conversation_id: str) -> bool:
        task = self._running.get(conversation_id)
        return task is not None and not task.done()

    def start(
        self,
        text: str,
        *,
        conversation_id: str | None = None,
        source: str = "web",
        model: str | None = None,
        task: str | None = None,
    ) -> str:
        # Erst prüfen, dann anlegen – sonst bliebe bei ungültiger Aufgabenart ein leeres Gespräch.
        kind = TaskKind(task) if task else TaskKind.CHAT
        conversation = self.app.conversations.ensure(conversation_id, channel="chat")
        if self.busy(conversation):
            raise RuntimeError("In diesem Gespräch läuft bereits eine Antwort")
        runner = asyncio.create_task(self._run(conversation, text, source, model, kind))
        self._running[conversation] = runner
        runner.add_done_callback(lambda _: self._running.pop(conversation, None))
        return conversation

    async def _run(
        self, conversation: str, text: str, source: str, model: str | None, kind: TaskKind
    ) -> None:
        try:
            await self.app.agent.ask(
                conversation, text, source=source, pinned_model=model or None, task=kind
            )
        except asyncio.CancelledError:
            self.app.events.publish("chat.cancelled", {}, session_id=conversation)
            raise
        except Exception as exc:
            log.exception("Chat-Lauf fehlgeschlagen")
            self.app.events.publish(
                "chat.error", {"error": f"Interner Fehler: {exc}"}, session_id=conversation
            )

    def cancel(self, conversation_id: str) -> bool:
        task = self._running.get(conversation_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True
