"""SQLite-Datenbank für strukturierte Daten (Kosten, später Projekte, Aufgaben, Aktionen).

Das Schema wird über durchnummerierte Migrationen gepflegt. Neue Phasen hängen
einfach weitere Einträge an :data:`MIGRATIONS` an – bestehende niemals ändern.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE model_usage (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                 TEXT    NOT NULL,
            day                TEXT    NOT NULL,
            month              TEXT    NOT NULL,
            provider           TEXT    NOT NULL,
            model              TEXT    NOT NULL,
            task               TEXT    NOT NULL,
            input_tokens       INTEGER NOT NULL DEFAULT 0,
            output_tokens      INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
            cache_write_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd           REAL    NOT NULL DEFAULT 0,
            latency_ms         INTEGER,
            success            INTEGER NOT NULL,
            estimated          INTEGER NOT NULL DEFAULT 0,
            error              TEXT,
            project_id         TEXT
        );
        CREATE INDEX idx_model_usage_day ON model_usage(day);
        CREATE INDEX idx_model_usage_month ON model_usage(month);
        CREATE INDEX idx_model_usage_project ON model_usage(project_id);
        """,
    ),
)

Params = Sequence[Any] | dict[str, Any]


class Database:
    """Dünne, threadsichere Hülle um eine SQLite-Verbindung (WAL-Modus)."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        # isolation_level=None: Autocommit; Transaktionen steuern wir selbst.
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")  # Dashboard kann parallel lesen
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")

    def migrate(self) -> int:
        """Spielt fehlende Migrationen ein und gibt die aktuelle Schemaversion zurück."""
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            row = self._conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
            current = int(row[0] or 0)
            for version, sql in MIGRATIONS:
                if version <= current:
                    continue
                script = (
                    f"BEGIN;\n{sql}\n"
                    f"INSERT INTO schema_version(version) VALUES ({version});\nCOMMIT;"
                )
                try:
                    self._conn.executescript(script)
                except sqlite3.Error:
                    if self._conn.in_transaction:
                        self._conn.execute("ROLLBACK")
                    raise
                current = version
            return current

    def execute(self, sql: str, params: Params = ()) -> int:
        """Führt eine schreibende Anweisung aus und liefert die ``lastrowid``."""
        with self._lock:
            cursor = self._conn.execute(sql, params)
            return int(cursor.lastrowid or 0)

    def query(self, sql: str, params: Params = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Params = ()) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self._conn.execute(sql, params).fetchone()
            return row

    def close(self) -> None:
        with self._lock:
            self._conn.close()
