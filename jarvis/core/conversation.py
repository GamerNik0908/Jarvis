"""Kurzzeitgedächtnis: der aktuelle Gesprächsverlauf.

Phase 1 kürzt den Verlauf einfach von vorne, wenn er zu lang wird. Ab Phase 4
werden ältere Teile stattdessen zusammengefasst und im Langzeitgedächtnis
abgelegt.
"""

from __future__ import annotations

from jarvis.router.base import ChatMessage


class Conversation:
    def __init__(self, *, max_messages: int = 40, max_chars: int = 100_000) -> None:
        self._max_messages = max_messages
        self._max_chars = max_chars
        self._messages: list[ChatMessage] = []

    @property
    def messages(self) -> list[ChatMessage]:
        return list(self._messages)

    def __len__(self) -> int:
        return len(self._messages)

    def add_user(self, text: str) -> None:
        self._messages.append(ChatMessage(role="user", content=text))
        self._trim()

    def add_assistant(self, text: str) -> None:
        self._messages.append(ChatMessage(role="assistant", content=text))
        self._trim()

    def rollback_last_user(self) -> None:
        """Entfernt eine unbeantwortete Nutzernachricht (nach Fehler oder Abbruch)."""
        if self._messages and self._messages[-1].role == "user":
            self._messages.pop()

    def clear(self) -> None:
        self._messages.clear()

    def _total_chars(self) -> int:
        return sum(len(m.content) for m in self._messages)

    def _trim(self) -> None:
        # Die neueste Nachricht bleibt immer erhalten, auch wenn sie allein zu lang ist.
        while len(self._messages) > 1 and (
            len(self._messages) > self._max_messages or self._total_chars() > self._max_chars
        ):
            self._messages.pop(0)
        # Die Modell-APIs erwarten, dass der Verlauf mit einer Nutzernachricht beginnt.
        while len(self._messages) > 1 and self._messages[0].role != "user":
            self._messages.pop(0)
