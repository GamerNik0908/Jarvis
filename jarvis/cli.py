"""Kommandozeile: ``jarvis [chat|ask|hw|models|cost|check|secrets]``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import asdict

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from jarvis import PHASE, __version__
from jarvis.core.app import JarvisApp, make_secret_store
from jarvis.core.config import ConfigError, LoadedConfig, load_config
from jarvis.core.hardware import detect_hardware, recommend_local_models, save_hardware_report
from jarvis.core.types import TaskKind
from jarvis.router.base import ChatMessage
from jarvis.router.errors import RouterError
from jarvis.security.secrets import SecretStoreError
from jarvis.ui.render import costs_view, hardware_view, models_view, response_notes, usage_line

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_INTERRUPTED = 130

_KNOWN_SECRET_NAMES = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "ELEVENLABS_API_KEY",
)
_SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jarvis", description="JARVIS – persönlicher KI-Assistent (Phase 1: Text-Chat)"
    )
    parser.add_argument(
        "--version", action="version", version=f"JARVIS {__version__} (Phase {PHASE})"
    )
    parser.add_argument("--config", help="Pfad zu einer alternativen config.yaml")
    parser.add_argument("--debug", action="store_true", help="Debug-Ausgaben im Terminal anzeigen")
    sub = parser.add_subparsers(dest="command", metavar="BEFEHL")

    sub.add_parser("chat", help="interaktiver Chat (Standard)")

    ask = sub.add_parser("ask", help="eine einzelne Frage stellen und beenden")
    ask.add_argument("prompt", nargs="+", help="die Frage")
    ask.add_argument("--task", choices=[t.value for t in TaskKind], default=TaskKind.CHAT.value)
    ask.add_argument("--model", help="bestimmtes Modell erzwingen (ohne Fallback)")

    hw = sub.add_parser("hw", help="Hardware erkennen und lokale Modelle empfehlen")
    hw.add_argument("--json", action="store_true", help="Ausgabe als JSON")

    sub.add_parser("models", help="Modelle, Status und Routing anzeigen")
    sub.add_parser("cost", help="Kosten heute und diesen Monat")

    check = sub.add_parser("check", help="Konfiguration, Keys und Budget prüfen")
    check.add_argument(
        "--live", action="store_true", help="zusätzlich eine Mini-Anfrage senden (kostet < $0.001)"
    )

    secrets = sub.add_parser("secrets", help="API-Keys im Windows Credential Manager verwalten")
    secrets_sub = secrets.add_subparsers(dest="secrets_command", metavar="AKTION", required=True)
    secrets_set = secrets_sub.add_parser("set", help="Key speichern (Eingabe bleibt unsichtbar)")
    secrets_set.add_argument("name", help="z. B. ANTHROPIC_API_KEY")
    secrets_delete = secrets_sub.add_parser("delete", help="Key löschen")
    secrets_delete.add_argument("name")
    secrets_sub.add_parser("status", help="zeigt, welche Keys wo hinterlegt sind (ohne Werte)")
    return parser


def _configure_stdio() -> None:
    """Sorgt unter Windows für UTF-8-Ausgabe, auch wenn die Ausgabe umgeleitet wird."""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    _configure_stdio()
    args = build_parser().parse_args(argv)
    console = Console(highlight=False)
    err = Console(stderr=True, highlight=False)
    command = args.command or "chat"
    try:
        if command == "secrets":
            return _cmd_secrets(load_config(args.config), args, console, err)
        if command == "hw":
            return _cmd_hw(load_config(args.config), args, console)
        app = JarvisApp.bootstrap(
            args.config,
            role="chat" if command == "chat" else "cli",
            console_level="DEBUG" if args.debug else None,
        )
    except ConfigError as exc:
        err.print(f"[red]Konfigurationsfehler:[/]\n{escape(str(exc))}")
        return EXIT_CONFIG
    try:
        return asyncio.run(_run_with_app(app, command, args, console, err))
    except KeyboardInterrupt:
        err.print("[yellow]Abgebrochen.[/]")
        return EXIT_INTERRUPTED


async def _run_with_app(
    app: JarvisApp, command: str, args: argparse.Namespace, console: Console, err: Console
) -> int:
    try:
        if command == "chat":
            from jarvis.ui.terminal import TerminalChat

            return await TerminalChat(app, console=console).run()
        if command == "ask":
            return await _cmd_ask(app, args, console, err)
        if command == "models":
            console.print(models_view(app.router))
            return EXIT_OK
        if command == "cost":
            console.print(costs_view(app.costs))
            return EXIT_OK
        if command == "check":
            return await _cmd_check(app, args, console)
        raise AssertionError(f"unbekannter Befehl {command}")  # pragma: no cover
    finally:
        await app.aclose()


async def _cmd_ask(app: JarvisApp, args: argparse.Namespace, console: Console, err: Console) -> int:
    prompt = " ".join(args.prompt).strip()
    final = None
    try:
        async for chunk in app.router.stream(
            [ChatMessage(role="user", content=prompt)],
            task=TaskKind(args.task),
            system=app.system_prompt(),
            pinned_model=args.model,
        ):
            if chunk.text:
                console.print(chunk.text, end="", markup=False, highlight=False, soft_wrap=True)
                console.file.flush()
            if chunk.response is not None:
                final = chunk.response
    except RouterError as exc:
        err.print(f"\n[red]⛔ {escape(str(exc))}[/]")
        return EXIT_ERROR
    console.print()
    if final is not None and app.config.ui.show_usage_line:
        status = app.costs.status()
        err.print(usage_line(final, status))
        for note in response_notes(final, status):
            err.print(note)
    return EXIT_OK


def _cmd_hw(loaded: LoadedConfig, args: argparse.Namespace, console: Console) -> int:
    data_dir = loaded.data_dir()
    hardware = detect_hardware(data_dir if data_dir.exists() else None)
    config = loaded.config.hardware
    recommendation = recommend_local_models(
        hardware, config.local_model_tiers, embedding_model=config.embedding_model
    )
    # Die Anzeige ist wichtiger als der gespeicherte Report.
    with contextlib.suppress(OSError):
        save_hardware_report(hardware, recommendation, data_dir / "hardware.json")
    if args.json:
        payload = {"hardware": asdict(hardware), "recommendation": asdict(recommendation)}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        console.print(hardware_view(hardware, recommendation))
    return EXIT_OK


async def _cmd_check(app: JarvisApp, args: argparse.Namespace, console: Console) -> int:
    ok = True
    info = Table(title="JARVIS-Check", show_header=False, title_justify="left")
    info.add_column(style="bold")
    info.add_column()
    info.add_row("Version", f"{__version__} (Phase {PHASE})")
    info.add_row("Konfiguration", str(app.loaded.path))
    info.add_row("Lokale Überschreibung", str(app.loaded.local_path or "–"))
    info.add_row("Datenordner", str(app.data_dir))
    info.add_row("Logdatei", str(app.log_file))
    status = app.costs.status()
    info.add_row("Budget", status.describe())
    if status.exceeded:
        ok = False
    console.print(info)

    providers = Table(title="Anbieter", title_justify="left")
    for column in ("Anbieter", "Key", "Quelle", "Status"):
        providers.add_column(column)
    for name, provider in app.router.providers.items():
        key_name = provider.api_key_name or "–"
        source = app.secrets.lookup(key_name)[1] if provider.api_key_name else None
        reason = provider.unavailable_reason()
        providers.add_row(
            name,
            key_name,
            {"env": "Umgebung/.env", "keyring": "Credential Manager"}.get(source or "", "–"),
            "[green]bereit[/]" if reason is None else f"[yellow]{escape(reason)}[/]",
        )
        if reason is not None and provider.config.enabled:
            ok = False
    console.print(providers)

    if args.live:
        ok = await _live_check(app, console) and ok
    console.print(
        "[green]Alles bereit.[/]" if ok else "[yellow]Es gibt offene Punkte (siehe oben).[/]"
    )
    return EXIT_OK if ok else EXIT_ERROR


async def _live_check(app: JarvisApp, console: Console) -> bool:
    """Sendet je Anbieter eine Mini-Anfrage an das günstigste nutzbare Modell."""
    success = True
    for name in app.router.providers:
        usable = [
            m
            for m in app.router.models()
            if m.provider == name and app.router.model_unavailable_reason(m) is None
        ]
        if not usable:
            continue
        cheapest = min(usable, key=lambda m: m.input_cost_per_mtok + m.output_cost_per_mtok)
        try:
            response = await app.router.complete(
                [ChatMessage(role="user", content="Antworte nur mit: OK")],
                task=TaskKind.SIMPLE,
                pinned_model=cheapest.id,
            )
        except RouterError as exc:
            console.print(f"[red]✘ {name}/{cheapest.id}: {escape(str(exc))}[/]")
            success = False
            continue
        console.print(
            f"[green]✔ {name}/{response.model}[/] antwortet "
            f"({response.latency_ms} ms, ${response.cost_usd:.5f}): {response.text.strip()[:40]!r}"
        )
    return success


def _cmd_secrets(
    loaded: LoadedConfig, args: argparse.Namespace, console: Console, err: Console
) -> int:
    store = make_secret_store(loaded)
    action = args.secrets_command
    if action == "status":
        names = list(
            dict.fromkeys(
                [p.api_key_name for p in loaded.config.router.providers.values() if p.api_key_name]
                + list(_KNOWN_SECRET_NAMES)
            )
        )
        table = Table(title="API-Keys (Werte werden nie angezeigt)", title_justify="left")
        table.add_column("Name")
        table.add_column("Quelle")
        for name in names:
            source = store.lookup(name)[1]
            label = {"env": "Umgebung/.env", "keyring": "Credential Manager"}.get(
                source or "", "[dim]nicht gesetzt[/]"
            )
            table.add_row(name, label)
        console.print(table)
        return EXIT_OK

    name = args.name.strip().upper()
    if not _SECRET_NAME.match(name):
        err.print("[red]Ungültiger Name – erlaubt sind Großbuchstaben, Ziffern und _.[/]")
        return EXIT_ERROR
    try:
        if action == "set":
            value = getpass.getpass(f"Wert für {name} (Eingabe unsichtbar): ")
            store.set(name, value)
            console.print(f"[green]{name} im Windows Credential Manager gespeichert.[/]")
            if store.lookup(name)[1] == "env":
                console.print(
                    f"[yellow]Hinweis: {name} ist zusätzlich als Umgebungsvariable bzw. in .env "
                    "gesetzt und hat dort Vorrang. Entferne den Eintrag, damit der neue Key "
                    "gilt.[/]"
                )
        elif action == "delete":
            removed = store.delete(name)
            console.print(
                f"[green]{name} gelöscht.[/]" if removed else f"{name} war nicht gespeichert."
            )
    except SecretStoreError as exc:
        err.print(f"[red]{escape(str(exc))}[/]")
        return EXIT_ERROR
    except (KeyboardInterrupt, EOFError):
        err.print("[yellow]Abgebrochen – nichts gespeichert.[/]")
        return EXIT_INTERRUPTED
    return EXIT_OK
