"""Text-Chat im Terminal mit Streaming, Befehlen und sauberem Abbruch per Strg+C."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
import threading
from collections.abc import Iterator
from types import FrameType
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory, InMemoryHistory
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from jarvis import PHASE, __version__
from jarvis.core.app import JarvisApp
from jarvis.core.hardware import detect_hardware, recommend_local_models, save_hardware_report
from jarvis.core.types import TaskKind
from jarvis.router.base import ChatResponse
from jarvis.router.errors import (
    BudgetExceededError,
    NoModelAvailableError,
    ProviderError,
)
from jarvis.ui.render import costs_view, hardware_view, models_view, response_notes, usage_line

log = logging.getLogger(__name__)

COMMANDS: tuple[tuple[str, str], ...] = (
    ("/hilfe", "diese Übersicht"),
    ("/neu", "Gesprächsverlauf leeren"),
    ("/modelle", "Modelle, Preise, Status und Routing anzeigen"),
    ("/nutze <id|auto>", "ein Modell fest wählen bzw. zurück zum automatischen Routing"),
    ("/aufgabe <art>", "Aufgabenart setzen: " + ", ".join(t.value for t in TaskKind)),
    ("/kosten", "Kosten heute und diesen Monat"),
    ("/hw", "Hardware und empfohlene lokale Modelle"),
    ("/ende", "JARVIS beenden (auch Strg+D)"),
)

_ALIASES = {
    "help": "hilfe",
    "?": "hilfe",
    "new": "neu",
    "clear": "neu",
    "models": "modelle",
    "use": "nutze",
    "task": "aufgabe",
    "cost": "kosten",
    "costs": "kosten",
    "hardware": "hw",
    "exit": "ende",
    "quit": "ende",
    "q": "ende",
}


@contextlib.contextmanager
def cancel_on_sigint(task: asyncio.Task[Any]) -> Iterator[None]:
    """Strg+C bricht während der Antwort nur ``task`` ab, nicht das ganze Programm.

    Der Handler wird nur für die Dauer einer Antwort installiert. Während der
    Eingabe verarbeitet prompt_toolkit Strg+C selbst.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    loop = asyncio.get_running_loop()

    def handler(signum: int, frame: FrameType | None) -> None:
        loop.call_soon_threadsafe(task.cancel)

    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


class TerminalChat:
    def __init__(self, app: JarvisApp, *, console: Console | None = None) -> None:
        self.app = app
        self.console = console or Console(highlight=False)
        self.conversation = app.new_conversation()
        self.task = TaskKind.CHAT
        self.pinned_model: str | None = None
        self._session: PromptSession[str] | None = None
        self._interactive = sys.stdin.isatty() and sys.stdout.isatty()

    # ------------------------------------------------------------------ Hauptschleife

    async def run(self) -> int:
        self._print_banner()
        await self._print_hardware(compact=True)
        self._print_readiness()
        while True:
            try:
                line = (await self._read_input()).strip()
            except KeyboardInterrupt:
                continue
            except EOFError:
                break
            if not line:
                continue
            if line.startswith("/"):
                if not await self._handle_command(line):
                    break
                continue
            await self._answer(line)
        self.console.print("[dim]Bis bald.[/]")
        return 0

    async def _read_input(self) -> str:
        if not self._interactive:
            return await asyncio.to_thread(input, "Du › ")
        if self._session is None:
            history = (
                FileHistory(str(self.app.data_dir / "chat_history.txt"))
                if self.app.config.ui.save_input_history
                else InMemoryHistory()
            )
            self._session = PromptSession(history=history)
        return await self._session.prompt_async("Du › ")

    # ------------------------------------------------------------------ Antworten

    async def _answer(self, text: str) -> None:
        self.conversation.add_user(text)
        task: asyncio.Task[ChatResponse] = asyncio.create_task(self._stream_answer())
        with cancel_on_sigint(task):
            try:
                response = await task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                self.conversation.rollback_last_user()
                self.console.print("\n[yellow]⏹ Abgebrochen.[/]")
                return
            except BudgetExceededError as exc:
                self.conversation.rollback_last_user()
                self.console.print(f"[red]⛔ {escape(str(exc))}[/]")
                return
            except NoModelAvailableError as exc:
                self.conversation.rollback_last_user()
                self.console.print(f"[red]⛔ {escape(str(exc))}[/]")
                if any("API-Key" in reason for reason in exc.reasons):
                    self.console.print(
                        "[dim]Key hinterlegen: [bold]jarvis secrets set ANTHROPIC_API_KEY[/bold] "
                        "(in einem zweiten Terminal), danach JARVIS neu starten.[/]"
                    )
                return
            except ProviderError as exc:
                self.conversation.rollback_last_user()
                self.console.print(f"\n[red]⛔ {exc.provider}: {escape(str(exc))}[/]")
                return
            except Exception as exc:  # der Chat soll bei Programmfehlern weiterlaufen
                log.exception("Unerwarteter Fehler bei der Antwort")
                self.conversation.rollback_last_user()
                self.console.print(
                    f"\n[red]⛔ Unerwarteter Fehler: {escape(type(exc).__name__)} – "
                    f"Details in {self.app.log_file}[/]"
                )
                return

        if response.text:
            self.conversation.add_assistant(response.text)
        else:
            self.conversation.rollback_last_user()
        if self.app.config.ui.show_usage_line:
            status = self.app.costs.status()
            self.console.print(usage_line(response, status))
            for note in response_notes(response, status):
                self.console.print(note)

    async def _stream_answer(self) -> ChatResponse:
        name = self.app.config.assistant.name
        final: ChatResponse | None = None
        started_output = False
        status = self.console.status(f"{name} denkt nach …", spinner="dots")
        status.start()
        try:
            async for chunk in self.app.router.stream(
                self.conversation.messages,
                task=self.task,
                system=self.app.system_prompt(),
                pinned_model=self.pinned_model,
            ):
                if chunk.text:
                    if not started_output:
                        status.stop()
                        self.console.print(f"[bold cyan]{name}[/] › ", end="")
                        started_output = True
                    self.console.print(
                        chunk.text, end="", markup=False, highlight=False, soft_wrap=True
                    )
                    self.console.file.flush()
                if chunk.response is not None:
                    final = chunk.response
        finally:
            status.stop()
            if started_output:
                self.console.print()
        if final is None:  # pragma: no cover - der Router garantiert eine Antwort
            raise RuntimeError("Keine Antwort erhalten")
        if not started_output:
            self.console.print(f"[bold cyan]{name}[/] › [dim](keine Textantwort)[/]")
        return final

    # ------------------------------------------------------------------ Befehle

    async def _handle_command(self, line: str) -> bool:
        """Führt einen /-Befehl aus. ``False`` beendet den Chat."""
        command, _, argument = line[1:].partition(" ")
        command = _ALIASES.get(command.lower(), command.lower())
        argument = argument.strip()
        if command == "ende":
            return False
        if command == "hilfe":
            self._print_help()
        elif command == "neu":
            self.conversation.clear()
            self.console.print("[green]Neues Gespräch begonnen.[/]")
        elif command == "modelle":
            self.console.print(models_view(self.app.router))
        elif command == "nutze":
            self._use_model(argument)
        elif command == "aufgabe":
            self._set_task(argument)
        elif command == "kosten":
            self.console.print(costs_view(self.app.costs))
        elif command == "hw":
            await self._print_hardware(compact=False)
        else:
            self.console.print(
                f"[yellow]Unbekannter Befehl /{escape(command)} – /hilfe zeigt alle.[/]"
            )
        return True

    def _use_model(self, argument: str) -> None:
        if not argument or argument.lower() == "auto":
            self.pinned_model = None
            self.console.print("[green]Automatisches Routing aktiv.[/]")
            return
        model = self.app.router.model(argument)
        if model is None:
            known = ", ".join(m.id for m in self.app.router.models())
            self.console.print(
                f"[yellow]Unbekanntes Modell '{escape(argument)}'. Bekannt: {known}[/]"
            )
            return
        reason = self.app.router.model_unavailable_reason(model)
        if reason:
            self.console.print(f"[yellow]{model.id} ist nicht nutzbar: {escape(reason)}[/]")
            return
        self.pinned_model = model.id
        self.console.print(f"[green]Feste Modellwahl: {model.label} (ohne Fallback).[/]")

    def _set_task(self, argument: str) -> None:
        try:
            self.task = TaskKind(argument.lower())
        except ValueError:
            options = ", ".join(t.value for t in TaskKind)
            self.console.print(
                f"[yellow]Aufgabenart '{escape(argument)}' unbekannt. Möglich: {options}[/]"
            )
            return
        chain = " → ".join(t.model for t in self.app.router.route(self.task))
        self.console.print(f"[green]Aufgabenart: {self.task.value} ({chain})[/]")

    # ------------------------------------------------------------------ Ausgaben

    def _print_banner(self) -> None:
        name = self.app.config.assistant.name
        self.console.rule(f"[bold cyan]{name}[/] · v{__version__} · Phase {PHASE}")
        self.console.print(
            "[dim]Tippe eine Nachricht oder /hilfe. Strg+C bricht eine laufende Antwort ab.[/]"
        )

    def _print_help(self) -> None:
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column(style="bold cyan")
        table.add_column()
        for command, description in COMMANDS:
            table.add_row(command, description)
        self.console.print(table)

    def _print_readiness(self) -> None:
        targets, skipped = self.app.router.plan(self.task)
        status = self.app.costs.status()
        if targets:
            self.console.print(f"[dim]Routing chat → {targets[0].model} · {status.describe()}[/]")
        else:
            self.console.print("[red]⛔ Kein Modell nutzbar:[/] " + escape("; ".join(skipped)))
            self.console.print(
                "[dim]API-Key hinterlegen: [bold]jarvis secrets set ANTHROPIC_API_KEY[/bold] "
                "oder in die Datei .env eintragen – dann JARVIS neu starten.[/]"
            )
        if status.exceeded:
            self.console.print(f"[red]⛔ {status.describe()}[/]")

    async def _print_hardware(self, *, compact: bool) -> None:
        hardware = await asyncio.to_thread(detect_hardware, self.app.data_dir)
        config = self.app.config.hardware
        recommendation = recommend_local_models(
            hardware, config.local_model_tiers, embedding_model=config.embedding_model
        )
        save_hardware_report(hardware, recommendation, self.app.data_dir / "hardware.json")
        if compact:
            self.console.print(f"[dim]Hardware: {hardware.summary()}[/]")
        else:
            self.console.print(hardware_view(hardware, recommendation))
