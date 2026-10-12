"""Dauerhaft gespeicherte Gespräche (Chat, Sprache, Projekte) für Verlauf und Dashboard."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from jarvis.core.db import Database
from jarvis.core.timeutil import now_iso


@dataclass(frozen=True)
class StoredMessage:
    id: int
    role: str
    content: str
    created: str
    meta: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "content": self.content,
            "created": self.created,
            "meta": self.meta,
        }


class ConversationStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create(self, *, channel: str = "chat", title: str = "") -> str:
        conversation_id = uuid.uuid4().hex[:12]
        stamp = now_iso()
        self._db.execute(
            "INSERT INTO conversations (id, title, channel, created, updated) VALUES (?, ?, ?, ?, ?)",
            (conversation_id, title, channel, stamp, stamp),
        )
        return conversation_id

    def exists(self, conversation_id: str) -> bool:
        return (
            self._db.query_one("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,))
            is not None
        )

    def ensure(self, conversation_id: str | None, *, channel: str = "chat") -> str:
        if conversation_id and self.exists(conversation_id):
            return conversation_id
        return self.create(channel=channel)

    def add(
        self, conversation_id: str, role: str, content: str, meta: dict[str, Any] | None = None
    ) -> int:
        stamp = now_iso()
        message_id = self._db.execute(
            "INSERT INTO messages (conversation_id, role, content, created, meta) VALUES (?, ?, ?, ?, ?)",
            (conversation_id, role, content, stamp, json.dumps(meta or {}, ensure_ascii=False)),
        )
        row = self._db.query_one("SELECT title FROM conversations WHERE id = ?", (conversation_id,))
        if row is not None and not row["title"] and role == "user":
            title = content.strip().splitlines()[0][:60] if content.strip() else "Gespräch"
            self._db.execute(
                "UPDATE conversations SET title = ?, updated = ? WHERE id = ?",
                (title, stamp, conversation_id),
            )
        else:
            self._db.execute(
                "UPDATE conversations SET updated = ? WHERE id = ?", (stamp, conversation_id)
            )
        return message_id

    def messages(self, conversation_id: str, *, limit: int = 200) -> list[StoredMessage]:
        rows = self._db.query(
            "SELECT * FROM (SELECT * FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?) "
            "ORDER BY id",
            (conversation_id, limit),
        )
        return [
            StoredMessage(
                r["id"], r["role"], r["content"], r["created"], json.loads(r["meta"] or "{}")
            )
            for r in rows
        ]

    def list(self, *, limit: int = 50, channel: str | None = None) -> list[dict[str, Any]]:
        if channel:
            rows = self._db.query(
                "SELECT * FROM conversations WHERE archived = 0 AND channel = ? "
                "ORDER BY updated DESC, rowid DESC LIMIT ?",
                (channel, limit),
            )
        else:
            rows = self._db.query(
                "SELECT * FROM conversations WHERE archived = 0 ORDER BY updated DESC, rowid DESC LIMIT ?",
                (limit,),
            )
        return [dict(r) for r in rows]

    def set_sensitive(self, conversation_id: str, sensitive: bool = True) -> None:
        self._db.execute(
            "UPDATE conversations SET sensitive = ? WHERE id = ?", (int(sensitive), conversation_id)
        )

    def is_sensitive(self, conversation_id: str) -> bool:
        row = self._db.query_one(
            "SELECT sensitive FROM conversations WHERE id = ?", (conversation_id,)
        )
        return bool(row and row[0])

    def rename(self, conversation_id: str, title: str) -> None:
        self._db.execute(
            "UPDATE conversations SET title = ? WHERE id = ?", (title, conversation_id)
        )

    def delete(self, conversation_id: str) -> None:
        self._db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
