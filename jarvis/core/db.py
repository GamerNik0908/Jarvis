"""SQLite-Datenbank für strukturierte Daten (Kosten, später Projekte, Aufgaben, Aktionen).

Das Schema wird über durchnummerierte Migrationen gepflegt. Neue Phasen hängen
einfach weitere Einträge an :data:`MIGRATIONS` an – bestehende niemals ändern.
"""

from __future__ import annotations

import logging
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
    (
        2,
        """
        CREATE TABLE audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            source      TEXT NOT NULL,
            session_id  TEXT,
            tool        TEXT NOT NULL,
            arguments   TEXT,
            level       TEXT NOT NULL,
            decision    TEXT NOT NULL,
            ok          INTEGER,
            result      TEXT,
            duration_ms INTEGER
        );
        CREATE INDEX idx_audit_ts ON audit_log(ts);

        CREATE TABLE conversations (
            id        TEXT PRIMARY KEY,
            title     TEXT NOT NULL DEFAULT '',
            channel   TEXT NOT NULL DEFAULT 'chat',
            created   TEXT NOT NULL,
            updated   TEXT NOT NULL,
            sensitive INTEGER NOT NULL DEFAULT 0,
            archived  INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE messages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            role            TEXT NOT NULL,
            content         TEXT NOT NULL,
            created         TEXT NOT NULL,
            meta            TEXT
        );
        CREATE INDEX idx_messages_conv ON messages(conversation_id, id);

        CREATE TABLE memories (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            text            TEXT NOT NULL,
            category        TEXT NOT NULL DEFAULT 'fakt',
            source          TEXT,
            created         TEXT NOT NULL,
            embedding       BLOB,
            embedding_model TEXT
        );

        CREATE TABLE reminders (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            text     TEXT NOT NULL,
            due      TEXT NOT NULL,
            repeat   TEXT,
            created  TEXT NOT NULL,
            done     INTEGER NOT NULL DEFAULT 0,
            fired_at TEXT
        );
        CREATE INDEX idx_reminders_due ON reminders(done, due);

        CREATE TABLE images (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            file        TEXT NOT NULL,
            kind        TEXT NOT NULL,
            prompt      TEXT,
            source_url  TEXT,
            attribution TEXT,
            created     TEXT NOT NULL,
            session_id  TEXT
        );

        CREATE TABLE cards (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            title   TEXT NOT NULL,
            content TEXT NOT NULL,
            image   TEXT,
            created TEXT NOT NULL,
            updated TEXT NOT NULL
        );

        CREATE TABLE projects (
            id          TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            goal        TEXT NOT NULL,
            status      TEXT NOT NULL,
            priority    INTEGER NOT NULL DEFAULT 3,
            budget_usd  REAL NOT NULL DEFAULT 0,
            plan        TEXT,
            summary     TEXT,
            created     TEXT NOT NULL,
            updated     TEXT NOT NULL,
            last_report TEXT
        );
        CREATE TABLE project_tasks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            position    INTEGER NOT NULL,
            milestone   TEXT,
            title       TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            kind        TEXT NOT NULL DEFAULT 'chat',
            status      TEXT NOT NULL DEFAULT 'pending',
            depends_on  TEXT,
            attempts    INTEGER NOT NULL DEFAULT 0,
            result      TEXT,
            feedback    TEXT,
            created     TEXT NOT NULL,
            updated     TEXT NOT NULL
        );
        CREATE INDEX idx_tasks_project ON project_tasks(project_id, position);
        CREATE TABLE project_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            ts         TEXT NOT NULL,
            kind       TEXT NOT NULL,
            text       TEXT NOT NULL
        );
        CREATE TABLE project_questions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            task_id    INTEGER,
            question   TEXT NOT NULL,
            answer     TEXT,
            status     TEXT NOT NULL DEFAULT 'open',
            created    TEXT NOT NULL,
            answered   TEXT
        );

        CREATE TABLE improvements (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            request       TEXT NOT NULL,
            source        TEXT NOT NULL DEFAULT 'user',
            status        TEXT NOT NULL,
            branch        TEXT,
            worktree      TEXT,
            base_commit   TEXT,
            result_commit TEXT,
            merge_commit  TEXT,
            session_id    TEXT,
            summary       TEXT,
            changed_files TEXT,
            diff_stat     TEXT,
            test_output   TEXT,
            error         TEXT,
            created       TEXT NOT NULL,
            updated       TEXT NOT NULL
        );
        CREATE TABLE improvement_suggestions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            description TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'open',
            created     TEXT NOT NULL
        );

        CREATE TABLE state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """,
    ),
)

#: Optionale Migrationen: schlagen sie fehl (z. B. SQLite ohne FTS5), läuft
#: JARVIS mit einfacherer Suche weiter.
OPTIONAL_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        3,
        """
        CREATE VIRTUAL TABLE memories_fts USING fts5(text, content='memories', content_rowid='id');
        CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, text) VALUES (new.id, new.text);
        END;
        CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.id, old.text);
        END;
        CREATE TRIGGER memories_au AFTER UPDATE OF text ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.id, old.text);
            INSERT INTO memories_fts(rowid, text) VALUES (new.id, new.text);
        END;
        """,
    ),
)

Params = Sequence[Any] | dict[str, Any]

log = logging.getLogger(__name__)


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
            steps = [(v, sql, False) for v, sql in MIGRATIONS]
            steps += [(v, sql, True) for v, sql in OPTIONAL_MIGRATIONS]
            for version, sql, optional in sorted(steps):
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
                    if not optional:
                        raise
                    log.warning("Optionale Migration %s übersprungen", version, exc_info=True)
                    self._conn.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
                current = version
            return current

    def has_table(self, name: str) -> bool:
        row = self.query_one(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?", (name,)
        )
        return row is not None

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM state WHERE key = ?", (key,))
        return str(row[0]) if row else default

    def set_state(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO state(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

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
