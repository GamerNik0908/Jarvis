"""Kleine Zeit-Helfer (immer lokale Zeit mit Zeitzone)."""

from __future__ import annotations

from datetime import datetime


def now() -> datetime:
    return datetime.now().astimezone()


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    """Liest ISO-8601; Zeiten ohne Zeitzone gelten als lokale Zeit."""
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def human(value: datetime) -> str:
    """z. B. 'Mo 12.10. 14:30'."""
    days = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
    local = value.astimezone()
    return f"{days[local.weekday()]} {local:%d.%m. %H:%M}"
