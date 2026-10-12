"""Chat im Terminal – mit Werkzeugen, Bestätigungen und Abbruch per Strg+C."""

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

from jarvis import __version__
from jarvis.core.app import JarvisApp
from jarvis.core.types import TaskKind
from jarvis.ui.render import costs_view, hardware_view, models_view

log = logging.getLogger(__name__)

COMMANDS: tuple[tuple[str, str], ...] = (
    ("/hilfe", "diese Übersicht"),
    ("/neu", "neues Gespräch beginnen"),
    ("/modelle", "Modelle, Status und Routing"),
    ("/nutze <id|auto>", "ein Modell fest wählen bzw. zurück zur automatischen Wahl"),
    ("/aufgabe <art>", "Aufgabenart: " + ", ".join(t.value for t in TaskKind)),
    ("/kosten", "Kostenübersicht"),
    ("/hw", "Hardware und empfohlene lokale Modelle"),
    ("/ende", "beenden (auch Strg+D)"),
)

_ALIASES = {
    "help": "hilfe", "?": "hilfe", "new": "neu", "clear": "neu", "models": "modelle", "use": "nutze",
    "task": "aufgabe", "cost": "kosten", "costs": "kosten", "hardware": "hw", "exit": "ende", "quit": "ende", "q": "ende",
}  # fmt: skip


@contextlib.contextmanager
def cancel_on_sigint(task: asyncio.Task[Any]) -> Iterator[None]:
    """Strg+C bricht während einer Antwort nur ``task`` ab, nicht das ganze Programm."""
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
        self.conversation = app.conversations.create(channel="terminal")
        self.task = TaskKind.CHAT
        self.pinned_model: str | None = None
        self._session: PromptSession[str] | None = None
        self._interactive = sys.stdin.isatty() and sys.stdout.isatty()

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        self.app.events.bind_loop(loop)
        self.app.killswitch.bind_loop(loop)
        self._print_banner()
        watcher = asyncio.create_task(self._watch_confirmations())
        try:
            while True:
                try:
                    line = (await self._read_input("Du › ")).strip()
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
        finally:
            watcher.cancel()
        self.console.print("[dim]Bis bald.[/]")
        return 0

    async def _read_input(self, prompt: str) -> str:
        if not self._interactive:
            return await asyncio.to_thread(input, prompt)
        if self._session is None:
            history = (
                FileHistory(str(self.app.data_dir / "chat_history.txt"))
                if self.app.config.ui.save_input_history
                else InMemoryHistory()
            )
            self._session = PromptSession(history=history)
        return await self._session.prompt_async(prompt)

    async def _watch_confirmations(self) -> None:
        """Bestätigungsanfragen der Werkzeuge direkt im Terminal beantworten."""
        with self.app.events.subscribe(types=["confirmation.requested"]) as subscription:
            async for event in subscription:
                data = event.data
                self.console.print(
                    f"\n[bold yellow]🔐 JARVIS möchte:[/] {escape(str(data.get('summary', '')))}"
                )
                try:
                    answer = await self._read_input("Erlauben? [j/N] ")
                except (KeyboardInterrupt, EOFError):
                    answer = "n"
                self.app.broker.resolve(
                    str(data["id"]), answer.strip().lower() in {"j", "ja", "y", "yes"}
                )

    async def _answer(self, text: str) -> None:
        task = asyncio.create_task(self._stream(text))
        with cancel_on_sigint(task):
            try:
                await task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                self.console.print("\n[yellow]⏹ Abgebrochen.[/]")
            except Exception as exc:
                log.exception("Unerwarteter Fehler bei der Antwort")
                self.console.print(
                    f"\n[red]⛔ Unerwarteter Fehler: {escape(type(exc).__name__)} – Details in {self.app.log_file}[/]"
                )

    async def _stream(self, text: str) -> None:
        name = self.app.config.assistant.name
        started = False
        status = self.console.status(f"{name} denkt nach …", spinner="dots")
        status.start()
        try:
            async for event in self.app.agent.run(
                self.conversation,
                text,
                task=self.task,
                source="terminal",
                pinned_model=self.pinned_model,
            ):
                if event.kind == "text":
                    if not started:
                        status.stop()
                        self.console.print(f"[bold cyan]{name}[/] › ", end="")
                        started = True
                    self.console.print(
                        event.text, end="", markup=False, highlight=False, soft_wrap=True
                    )
                    self.console.file.flush()
                elif event.kind == "tool":
                    status.stop()
                    self.console.print(f"\n[dim]🔧 {escape(event.text)}[/]")
                    started = False
                    status.start()
                elif event.kind == "error":
                    status.stop()
                    self.console.print(f"\n[red]⛔ {escape(event.text)}[/]")
                elif event.kind == "final":
                    status.stop()
                    if not started and event.text:
                        self.console.print(f"[bold cyan]{name}[/] › {escape(event.text)}", end="")
                    self.console.print()
                    for image in event.data.get("images", []):
                        self.console.print(
                            f"[dim]🖼️  Bild: {escape(str(image.get('path') or image.get('url')))}[/]"
                        )
                    if self.app.config.ui.show_usage_line and event.response is not None:
                        response = event.response
                        cost = "Abo" if response.subscription else f"${response.cost_usd:.4f}"
                        self.console.print(
                            f"[dim]{escape(response.model)} · {response.input_tokens} → {response.output_tokens} Tokens · {cost}[/]"
                        )
        finally:
            status.stop()

    async def _handle_command(self, line: str) -> bool:
        command, _, argument = line[1:].partition(" ")
        command = _ALIASES.get(command.lower(), command.lower())
        argument = argument.strip()
        if command == "ende":
            return False
        if command == "hilfe":
            table = Table(show_header=False, box=None, padding=(0, 2))
            table.add_column(style="bold cyan")
            table.add_column()
            for name, description in COMMANDS:
                table.add_row(name, description)
            self.console.print(table)
        elif command == "neu":
            self.conversation = self.app.conversations.create(channel="terminal")
            self.console.print("[green]Neues Gespräch begonnen.[/]")
        elif command == "modelle":
            self.console.print(models_view(self.app.router))
        elif command == "nutze":
            self._use_model(argument)
        elif command == "aufgabe":
            try:
                self.task = TaskKind(argument.lower())
                self.console.print(f"[green]Aufgabenart: {self.task.value}[/]")
            except ValueError:
                self.console.print(
                    f"[yellow]Unbekannt. Möglich: {', '.join(t.value for t in TaskKind)}[/]"
                )
        elif command == "kosten":
            self.console.print(costs_view(self.app.costs))
        elif command == "hw":
            if self.app.hardware and self.app.recommendation:
                self.console.print(hardware_view(self.app.hardware, self.app.recommendation))
        else:
            self.console.print(
                f"[yellow]Unbekannter Befehl /{escape(command)} – /hilfe zeigt alle.[/]"
            )
        return True

    def _use_model(self, argument: str) -> None:
        if not argument or argument.lower() == "auto":
            self.pinned_model = None
            self.console.print("[green]Automatische Modellwahl aktiv.[/]")
            return
        model = self.app.router.model(argument)
        if model is None:
            known = ", ".join(m.id for m in self.app.router.models())
            self.console.print(f"[yellow]Unbekanntes Modell. Bekannt: {known}[/]")
            return
        reason = self.app.router.model_unavailable_reason(model)
        if reason:
            self.console.print(f"[yellow]{model.id} ist nicht nutzbar: {escape(reason)}[/]")
            return
        self.pinned_model = model.id
        self.console.print(f"[green]Feste Modellwahl: {model.label}[/]")

    def _print_banner(self) -> None:
        name = self.app.config.assistant.name
        self.console.rule(f"[bold cyan]{name}[/] · v{__version__} · Terminal")
        ready = [
            m.label
            for m in self.app.router.models()
            if self.app.router.model_unavailable_reason(m) is None
        ]
        if ready:
            self.console.print(
                f"[dim]Bereit: {', '.join(ready)} · /hilfe für Befehle · Strg+C bricht eine Antwort ab[/]"
            )
        else:
            self.console.print(
                "[red]⛔ Noch kein KI-Modell bereit.[/] [dim]Führe 'jarvis setup' aus.[/]"
            )
        if self.app.hardware:
            self.console.print(f"[dim]Hardware: {self.app.hardware.summary()}[/]")
