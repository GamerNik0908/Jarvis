"""Zeitgesteuerte Aufgaben: Erinnerungen, Tagesbericht, Selbst-Check, Anbieter-Prüfungen."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from jarvis.core.tasks import spawn
from jarvis.core.timeutil import human, now
from jarvis.router.providers.claude_code_provider import ClaudeCodeProvider
from jarvis.router.providers.ollama_provider import OllamaProvider

if TYPE_CHECKING:
    from jarvis.core.app import JarvisApp

log = logging.getLogger(__name__)

TICK_S = 15


def parse_hhmm(value: str) -> tuple[int, int]:
    hour, _, minute = value.partition(":")
    return max(0, min(int(hour), 23)), max(0, min(int(minute or 0), 59))


def is_due(moment: datetime, at: str, last_run_day: str | None, *, weekly: bool = False) -> bool:
    """Ist die tägliche (oder wöchentliche) Aufgabe um ``at`` jetzt fällig?"""
    hour, minute = parse_hhmm(at)
    today = moment.strftime("%Y-%m-%d")
    if last_run_day == today:
        return False
    if (moment.hour, moment.minute) < (hour, minute):
        return False
    if weekly and last_run_day:
        try:
            last = datetime.strptime(last_run_day, "%Y-%m-%d")
        except ValueError:
            return True
        return (moment.replace(tzinfo=None) - last).days >= 7
    return True


class Scheduler:
    def __init__(self, app: JarvisApp) -> None:
        self.app = app
        self._task: asyncio.Task[None] | None = None
        self._last_claude_probe = 0.0

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="scheduler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Fehler im Zeitplaner")
            await asyncio.sleep(TICK_S)

    async def tick(self, moment: datetime | None = None) -> None:
        moment = moment or now()
        self.fire_reminders(moment)
        await self._refresh_providers()
        db = self.app.db
        if self.app.projects is not None:
            last = db.get_state("daily_report_day")
            if is_due(moment, self.app.config.projects.daily_report_time, last):
                db.set_state("daily_report_day", moment.strftime("%Y-%m-%d"))
                report = self.app.projects.daily_report(moment)
                if "keine aktiven Projekte" not in report:
                    self.app.notify(
                        "Tagesbericht", "Dein Projekt-Tagesbericht liegt auf der Übersicht."
                    )
        improver = self.app.improvements
        mode = self.app.config.self_improve.self_check
        if improver is not None and mode != "off":
            last = db.get_state("self_check_day")
            if is_due(
                moment, self.app.config.self_improve.self_check_time, last, weekly=mode == "weekly"
            ):
                db.set_state("self_check_day", moment.strftime("%Y-%m-%d"))
                spawn(improver.self_check(), name="self-check")

    def fire_reminders(self, moment: datetime) -> None:
        for reminder in self.app.reminders.pop_due(moment):
            self.app.events.publish("reminder.due", reminder.to_dict())
            self.app.notify(
                "Erinnerung",
                f"{reminder.text} ({human(reminder.due)})",
                speak=True,
            )

    async def _refresh_providers(self) -> None:
        loop = asyncio.get_running_loop()
        for provider in self.app.router.providers.values():
            if not provider.config.enabled:
                continue
            if isinstance(provider, OllamaProvider) and provider.probe_stale:
                await provider.probe()
            elif (
                isinstance(provider, ClaudeCodeProvider)
                and provider.unavailable_reason() is not None
                and loop.time() - self._last_claude_probe > 300
            ):
                self._last_claude_probe = loop.time()
                await provider.probe()
