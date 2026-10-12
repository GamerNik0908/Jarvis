"""Kleine Datenspeicher: Erinnerungen, Übersichtskarten und Bildergalerie."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jarvis.core.db import Database
from jarvis.core.timeutil import now, now_iso, parse_iso

REPEATS = ("daily", "weekdays", "weekly", "monthly")


@dataclass(frozen=True)
class Reminder:
    id: int
    text: str
    due: datetime
    repeat: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "due": self.due.isoformat(timespec="minutes"),
            "repeat": self.repeat,
        }


def next_occurrence(due: datetime, repeat: str) -> datetime:
    """Nächster Termin einer Wiederholung.

    Gerechnet wird in lokaler Uhrzeit: Eine tägliche Erinnerung um 8:00 bleibt auch
    nach der Umstellung zwischen Sommer- und Winterzeit bei 8:00.
    """
    if due.tzinfo is not None:
        return _next_local(due.astimezone().replace(tzinfo=None), repeat).astimezone()
    return _next_local(due, repeat)


def _next_local(due: datetime, repeat: str) -> datetime:
    if repeat == "daily":
        return due + timedelta(days=1)
    if repeat == "weekly":
        return due + timedelta(weeks=1)
    if repeat == "weekdays":
        candidate = due + timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate
    if repeat == "monthly":
        month = due.month + 1
        year = due.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        day = min(due.day, 28)
        return due.replace(year=year, month=month, day=day)
    raise ValueError(f"Unbekannte Wiederholung: {repeat}")


class ReminderStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create(self, text: str, due: datetime, repeat: str | None = None) -> Reminder:
        if repeat is not None and repeat not in REPEATS:
            raise ValueError(f"Wiederholung muss eine von {REPEATS} sein")
        due = due if due.tzinfo else due.astimezone()
        reminder_id = self._db.execute(
            "INSERT INTO reminders (text, due, repeat, created) VALUES (?, ?, ?, ?)",
            (text, due.isoformat(timespec="seconds"), repeat, now_iso()),
        )
        return Reminder(reminder_id, text, due, repeat)

    def upcoming(self, limit: int = 50) -> list[Reminder]:
        rows = self._db.query(
            "SELECT * FROM reminders WHERE done = 0 ORDER BY due LIMIT ?", (limit,)
        )
        return [Reminder(r["id"], r["text"], parse_iso(r["due"]), r["repeat"]) for r in rows]

    def delete(self, reminder_id: int) -> bool:
        row = self._db.query_one("SELECT 1 FROM reminders WHERE id = ?", (reminder_id,))
        if row is None:
            return False
        self._db.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        return True

    def pop_due(self, at: datetime | None = None) -> list[Reminder]:
        """Liefert fällige Erinnerungen und plant Wiederholungen neu ein."""
        moment = at or now()
        due = [r for r in self.upcoming(500) if r.due <= moment]
        for reminder in due:
            if reminder.repeat:
                following = next_occurrence(reminder.due, reminder.repeat)
                while following <= moment:
                    following = next_occurrence(following, reminder.repeat)
                self._db.execute(
                    "UPDATE reminders SET due = ?, fired_at = ? WHERE id = ?",
                    (following.isoformat(timespec="seconds"), now_iso(), reminder.id),
                )
            else:
                self._db.execute(
                    "UPDATE reminders SET done = 1, fired_at = ? WHERE id = ?",
                    (now_iso(), reminder.id),
                )
        return due


class CardStore:
    """Karten auf der Übersicht – JARVIS kann dort Dinge für dich anpinnen."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def upsert(self, title: str, content: str, image: str | None = None) -> int:
        stamp = now_iso()
        row = self._db.query_one("SELECT id FROM cards WHERE lower(title) = lower(?)", (title,))
        if row is not None:
            self._db.execute(
                "UPDATE cards SET content = ?, image = ?, updated = ? WHERE id = ?",
                (content, image, stamp, row[0]),
            )
            return int(row[0])
        return self._db.execute(
            "INSERT INTO cards (title, content, image, created, updated) VALUES (?, ?, ?, ?, ?)",
            (title, content, image, stamp, stamp),
        )

    def list(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._db.query("SELECT * FROM cards ORDER BY updated DESC")]

    def delete(self, card_id: int) -> bool:
        if self._db.query_one("SELECT 1 FROM cards WHERE id = ?", (card_id,)) is None:
            return False
        self._db.execute("DELETE FROM cards WHERE id = ?", (card_id,))
        return True


_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug or "bild")[:limit]


class ImageStore:
    """Bilder in ``<Datenordner>/images`` plus Eintrag in der Galerie."""

    def __init__(self, db: Database, directory: Path) -> None:
        self._db = db
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)

    def save(
        self,
        data: bytes,
        *,
        content_type: str,
        kind: str,
        prompt: str = "",
        source_url: str | None = None,
        attribution: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        extension = _EXTENSIONS.get(content_type.split(";")[0].strip().lower(), ".jpg")
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{slugify(prompt)}-{time.monotonic_ns() % 10000}{extension}"
        (self.directory / name).write_bytes(data)
        image_id = self._db.execute(
            "INSERT INTO images (file, kind, prompt, source_url, attribution, created, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, kind, prompt, source_url, attribution, now_iso(), session_id),
        )
        return {
            "id": image_id,
            "file": name,
            "url": f"/api/images/file/{name}",
            "path": str(self.directory / name),
        }

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._db.query("SELECT * FROM images ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) | {"url": f"/api/images/file/{r['file']}"} for r in rows]

    def path_for(self, name: str) -> Path | None:
        if "/" in name or "\\" in name or name.startswith("."):
            return None
        path = self.directory / name
        return path if path.is_file() else None

    def delete(self, image_id: int) -> bool:
        row = self._db.query_one("SELECT file FROM images WHERE id = ?", (image_id,))
        if row is None:
            return False
        path = self.path_for(row["file"])
        if path is not None:
            path.unlink(missing_ok=True)
        self._db.execute("DELETE FROM images WHERE id = ?", (image_id,))
        return True
