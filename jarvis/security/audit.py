"""Vollständiges Aktionsprotokoll: jeder Werkzeug-Aufruf mit Entscheidung und Ergebnis."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from jarvis.core.db import Database
from jarvis.core.timeutil import now_iso
from jarvis.security.redaction import redact

_ARGS_LIMIT = 2_000
_RESULT_LIMIT = 600


@dataclass(frozen=True)
class AuditEntry:
    source: str
    tool: str
    arguments: dict[str, Any]
    level: str
    decision: str
    ok: bool | None = None
    result: str = ""
    duration_ms: int | None = None
    session_id: str | None = None


class AuditLog:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record(self, entry: AuditEntry) -> int:
        arguments = redact(json.dumps(entry.arguments, ensure_ascii=False, default=str))
        return self._db.execute(
            """
            INSERT INTO audit_log (ts, source, session_id, tool, arguments, level, decision,
                                   ok, result, duration_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_iso(),
                entry.source,
                entry.session_id,
                entry.tool,
                arguments[:_ARGS_LIMIT],
                entry.level,
                entry.decision,
                None if entry.ok is None else int(entry.ok),
                redact(entry.result)[:_RESULT_LIMIT],
                entry.duration_ms,
            ),
        )

    def recent(self, limit: int = 100, *, tool: str | None = None) -> list[dict[str, Any]]:
        if tool:
            rows = self._db.query(
                "SELECT * FROM audit_log WHERE tool = ? ORDER BY id DESC LIMIT ?", (tool, limit)
            )
        else:
            rows = self._db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(row) for row in rows]

    def failures_since(self, iso_ts: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM audit_log WHERE ts >= ? AND ok = 0 ORDER BY id DESC LIMIT ?",
            (iso_ts, limit),
        )
        return [dict(row) for row in rows]
