"""Kommandozeile von JARVIS.

Wichtigste Befehle:

* ``jarvis`` – startet JARVIS im Hintergrund (falls nötig) und öffnet das Fenster
* ``jarvis setup`` – geführte Einrichtung (alles kostenlos)
* ``jarvis chat`` – Chat im Terminal
* ``jarvis stop`` / ``jarvis status`` / ``jarvis autostart on|off``
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

import httpx
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from jarvis import __version__
from jarvis.core import autostart
from jarvis.core.app import JarvisApp, make_secret_store
from jarvis.core.config import PROJECT_ROOT, ConfigError, LoadedConfig, load_config
from jarvis.core.hardware import detect_hardware, recommend_local_models, save_hardware_report
from jarvis.core.types import TaskKind
from jarvis.security.secrets import SecretStoreError
from jarvis.ui.render import costs_view, hardware_view, models_view, usage_line

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_INTERRUPTED = 130

_KNOWN_SECRET_NAMES = (
    "GEMINI_API_KEY",
    "POLLINATIONS_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
)
_SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jarvis", description="JARVIS – persönlicher KI-Assistent"
    )
    parser.add_argument("--version", action="version", version=f"JARVIS {__version__}")
    parser.add_argument("--config", help="Pfad zu einer alternativen config.yaml")
    parser.add_argument("--debug", action="store_true", help="Debug-Ausgaben im Terminal anzeigen")
    sub = parser.add_subparsers(dest="command", metavar="BEFEHL")

    sub.add_parser("start", help="JARVIS im Hintergrund starten und Fenster öffnen (Standard)")
    run = sub.add_parser("run", help="Supervisor + Dienst im Vordergrund (für Autostart)")
    run.add_argument("--background", action="store_true", help="kein Fenster beim Start öffnen")
    service = sub.add_parser("service", help=argparse.SUPPRESS)
    service.add_argument("--background", action="store_true")
    sub.add_parser("stop", help="laufenden JARVIS beenden")
    sub.add_parser("status", help="zeigt, ob JARVIS läuft")
    window = sub.add_parser("window", help="Chat-Fenster öffnen")
    window.add_argument("--url", help=argparse.SUPPRESS)
    auto = sub.add_parser("autostart", help="Autostart bei der Windows-Anmeldung")
    auto.add_argument("action", choices=["on", "off", "status"])
    sub.add_parser("setup", help="geführte Einrichtung")

    sub.add_parser("chat", help="Chat im Terminal")
    ask = sub.add_parser("ask", help="eine einzelne Frage stellen")
    ask.add_argument("prompt", nargs="+")
    ask.add_argument("--task", choices=[t.value for t in TaskKind], default=TaskKind.CHAT.value)
    ask.add_argument("--model", help="bestimmtes Modell erzwingen")
    ask.add_argument("--no-tools", action="store_true", help="ohne Werkzeuge antworten")
    improve = sub.add_parser("improve", help="JARVIS etwas Neues beibringen (Selbstverbesserung)")
    improve.add_argument("request", nargs="+")

    hw = sub.add_parser("hw", help="Hardware erkennen und lokale Modelle empfehlen")
    hw.add_argument("--json", action="store_true")
    sub.add_parser("models", help="Modelle, Status und Routing")
    sub.add_parser("cost", help="Kostenübersicht")
    check = sub.add_parser("check", help="Konfiguration und Anbieter prüfen")
    check.add_argument("--live", action="store_true", help="zusätzlich eine Mini-Anfrage senden")

    secrets = sub.add_parser("secrets", help="API-Keys im Windows Credential Manager verwalten")
    secrets_sub = secrets.add_subparsers(dest="secrets_command", metavar="AKTION", required=True)
    secrets_set = secrets_sub.add_parser("set", help="Key speichern (Eingabe unsichtbar)")
    secrets_set.add_argument("name")
    secrets_delete = secrets_sub.add_parser("delete", help="Key löschen")
    secrets_delete.add_argument("name")
    secrets_sub.add_parser("status", help="welche Keys wo hinterlegt sind (ohne Werte)")

    google = sub.add_parser("google", help="Gmail-Konten und Kalender verbinden")
    google_sub = google.add_subparsers(dest="google_command", metavar="AKTION", required=True)
    google_sub.add_parser("add", help="Google-Konto verbinden (öffnet den Browser)")
    google_sub.add_parser("list", help="verbundene Konten")
    google_remove = google_sub.add_parser("remove", help="Konto trennen")
    google_remove.add_argument("email")

    sub.add_parser("mcp-bridge", help=argparse.SUPPRESS)
    return parser


def _configure_stdio() -> None:
    """Benutzbare Standard-Streams und UTF-8-Ausgabe unter Windows.

    Unter ``pythonw.exe`` (Desktop-Verknüpfung, Autostart, Supervisor und Dienst) sind
    ``sys.stdout``/``sys.stderr`` ``None``. Bibliotheken wie uvicorn (``sys.stdout.isatty()``
    beim Einrichten des Loggings) oder tqdm (Fortschritt beim Modell-Download) stürzen dann
    ab – deshalb schreiben sie in diesem Fall nach ``os.devnull``. Protokolliert wird
    ohnehin in die Logdatei.
    """
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 – bis Prozessende
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 – bis Prozessende
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(Exception):
                reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    _configure_stdio()
    args = build_parser().parse_args(argv)
    console = Console(highlight=False)
    err = Console(stderr=True, highlight=False)
    command = args.command or "start"
    try:
        if command == "mcp-bridge":
            return _cmd_mcp_bridge()
        if command == "window":
            return _cmd_window(args)
        loaded = load_config(args.config)
        if command == "secrets":
            return _cmd_secrets(loaded, args, console, err)
        if command == "hw":
            return _cmd_hw(loaded, args, console)
        if command == "autostart":
            return _cmd_autostart(args, console)
        if command == "start":
            return _cmd_start(loaded, args, console)
        if command == "stop":
            return _cmd_remote(loaded, "/api/system/shutdown", console, "JARVIS wird beendet.")
        if command == "status":
            return _cmd_status(loaded, console)
        if command == "improve":
            return _cmd_improve(loaded, args, console, err)
        if command == "run":
            from jarvis.core.logging_setup import setup_logging
            from jarvis.core.supervisor import supervise

            setup_logging(loaded.config.logging, loaded.data_dir() / "logs", role="supervisor")
            return supervise(PROJECT_ROOT, loaded.data_dir(), config_path=args.config)
        if command == "google":
            return _cmd_google(loaded, args, console, err)
        role = {"service": "service", "chat": "chat"}.get(command, "cli")
        app = JarvisApp.bootstrap(
            args.config, role=role, console_level="DEBUG" if args.debug else None
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
        if command == "service":
            from jarvis.core.service import JarvisService

            return await JarvisService(app, background=args.background).run()
        if command == "setup":
            from jarvis.ui.setup_wizard import run_setup

            return await run_setup(app, console)
        if command == "chat":
            from jarvis.ui.terminal import TerminalChat

            await app.startup_probes()
            return await TerminalChat(app, console=console).run()
        if command == "ask":
            return await _cmd_ask(app, args, console, err)
        if command == "models":
            await app.startup_probes()
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


# ---------------------------------------------------------------------- Dienst steuern


def _service_url(loaded: LoadedConfig) -> str:
    return f"http://127.0.0.1:{loaded.config.web.port}"


def _service_running(loaded: LoadedConfig) -> bool:
    try:
        response = httpx.get(_service_url(loaded) + "/", timeout=2)
        return response.status_code == 200 and "JARVIS" in response.text
    except httpx.HTTPError:
        return False


def _token(loaded: LoadedConfig) -> str | None:
    path = loaded.data_dir() / "api_token"
    return path.read_text(encoding="utf-8").strip() if path.is_file() else None


def _pythonw() -> str:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.is_file() else exe)


def _cmd_start(loaded: LoadedConfig, args: argparse.Namespace, console: Console) -> int:
    from jarvis.ui.desktop import open_window

    url = _service_url(loaded) + "/"
    if _service_running(loaded):
        open_window(url)
        console.print("JARVIS läuft bereits – Fenster wird geöffnet.")
        return EXIT_OK
    command = [_pythonw(), "-m", "jarvis"]
    if args.config:
        command += ["--config", args.config]
    command += ["run"]
    flags = 0
    if sys.platform == "win32":
        flags = int(getattr(subprocess, "DETACHED_PROCESS", 0)) | int(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    logs = loaded.data_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    start_log = logs / "start.log"
    # Alles, was der Hintergrundprozess vor dem eigenen Logging ausgibt (z. B. ein Absturz beim
    # Import), landet hier – sonst wäre ein Startfehler unter pythonw.exe unsichtbar.
    with start_log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            creationflags=flags,
            close_fds=True,
            start_new_session=sys.platform != "win32",
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    console.print("JARVIS startet im Hintergrund … (beim ersten Start bis zu 2 Minuten)")
    for _ in range(240):
        if _service_running(loaded):
            console.print(f"[green]Bereit:[/] {url}  (das Fenster öffnet sich automatisch)")
            return EXIT_OK
        if process.poll() is not None:
            break
        time.sleep(0.5)
    _report_start_problem(console, logs, stopped=process.poll() is not None)
    return EXIT_ERROR


def _tail(path: Path, lines: int) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def _report_start_problem(console: Console, logs: Path, *, stopped: bool) -> None:
    """Zeigt nach einem gescheiterten Start die letzten Logzeilen direkt im Terminal."""
    if stopped:
        console.print("[red]JARVIS konnte nicht starten.[/] Letzte Meldungen:")
    else:
        console.print("[yellow]JARVIS antwortet noch nicht.[/] Letzte Meldungen:")
    shown = False
    for name in ("start.log", "jarvis-supervisor.log", "jarvis-service.log"):
        text = _tail(logs / name, 25).strip()
        if text:
            shown = True
            console.print(f"[bold]── {name}[/]")
            console.print(escape(text), highlight=False)
    if not shown:
        console.print("(keine Meldungen)")
    console.print(
        f"\nLogordner: {logs}\n"
        "Zum Prüfen im Vordergrund starten (zeigt Fehler sofort): [bold].\\scripts\\jarvis.cmd run[/]"
    )


def _cmd_remote(loaded: LoadedConfig, path: str, console: Console, message: str) -> int:
    token = _token(loaded)
    if not token or not _service_running(loaded):
        console.print("JARVIS läuft nicht.")
        return EXIT_OK
    httpx.post(_service_url(loaded) + path, headers={"X-Jarvis-Token": token}, timeout=10)
    console.print(message)
    return EXIT_OK


def _cmd_status(loaded: LoadedConfig, console: Console) -> int:
    running = _service_running(loaded)
    console.print(
        f"JARVIS {__version__}: "
        + (
            "[green]läuft[/] – " + _service_url(loaded)
            if running
            else "[yellow]läuft nicht[/] – starten mit 'jarvis'"
        )
    )
    console.print(f"Autostart: {'an' if autostart.is_enabled() else 'aus'}")
    console.print(f"Datenordner: {loaded.data_dir()}")
    return EXIT_OK if running else EXIT_ERROR


def _cmd_window(args: argparse.Namespace) -> int:
    from jarvis.ui.desktop import run_window

    url = args.url
    if not url:
        loaded = load_config(None)
        url = _service_url(loaded) + "/"
    return run_window(url)


def _cmd_autostart(args: argparse.Namespace, console: Console) -> int:
    if args.action == "on":
        try:
            value = autostart.enable(PROJECT_ROOT)
        except RuntimeError as exc:
            console.print(f"[yellow]{exc}[/]")
            return EXIT_ERROR
        console.print(f"[green]Autostart aktiv.[/] JARVIS startet bei jeder Anmeldung: {value}")
    elif args.action == "off":
        console.print(
            "Autostart entfernt." if autostart.disable() else "Autostart war nicht aktiv."
        )
    else:
        console.print("Autostart ist " + ("an." if autostart.is_enabled() else "aus."))
    return EXIT_OK


def _cmd_improve(
    loaded: LoadedConfig, args: argparse.Namespace, console: Console, err: Console
) -> int:
    token = _token(loaded)
    if not token or not _service_running(loaded):
        err.print("JARVIS muss laufen (starte ihn mit 'jarvis').")
        return EXIT_ERROR
    response = httpx.post(
        _service_url(loaded) + "/api/improvements",
        headers={"X-Jarvis-Token": token},
        json={"request": " ".join(args.request)},
        timeout=10,
    )
    if response.status_code != 200:
        err.print(f"[red]{escape(response.text)}[/]")
        return EXIT_ERROR
    job = response.json()
    console.print(
        f"[green]Verbesserung #{job['id']} gestartet.[/] Fortschritt: {_service_url(loaded)}/#/improve/{job['id']}"
    )
    return EXIT_OK


def _cmd_mcp_bridge() -> int:
    from jarvis.integrations.mcp_bridge import serve_stdio

    serve_stdio(
        os.environ.get("JARVIS_API", "http://127.0.0.1:8765"),
        os.environ.get("JARVIS_API_TOKEN", ""),
        os.environ.get("JARVIS_SESSION", ""),
        [n for n in os.environ.get("JARVIS_TOOLS", "").split(",") if n],
    )
    return EXIT_OK


# ---------------------------------------------------------------------- Einzelbefehle


async def _cmd_ask(app: JarvisApp, args: argparse.Namespace, console: Console, err: Console) -> int:
    await app.startup_probes()
    prompt = " ".join(args.prompt).strip()
    conversation = app.conversations.create(channel="cli")
    final = None
    async for event in app.agent.run(
        conversation,
        prompt,
        task=TaskKind(args.task),
        source="cli",
        pinned_model=args.model,
        use_tools=not args.no_tools,
    ):
        if event.kind == "text":
            console.print(event.text, end="", markup=False, highlight=False, soft_wrap=True)
            console.file.flush()
        elif event.kind == "tool":
            err.print(f"\n[dim]🔧 {escape(event.text)}[/]")
        elif event.kind == "error":
            err.print(f"\n[red]⛔ {escape(event.text)}[/]")
            return EXIT_ERROR
        elif event.kind == "final":
            final = event.response
    console.print()
    if final is not None and app.config.ui.show_usage_line:
        err.print(usage_line(final, app.costs.status()))
    return EXIT_OK


def _cmd_hw(loaded: LoadedConfig, args: argparse.Namespace, console: Console) -> int:
    data_dir = loaded.data_dir()
    hardware = detect_hardware(data_dir if data_dir.exists() else None)
    config = loaded.config.hardware
    recommendation = recommend_local_models(
        hardware, config.local_model_tiers, embedding_model=config.embedding_model
    )
    with contextlib.suppress(OSError):
        save_hardware_report(hardware, recommendation, data_dir / "hardware.json")
    if args.json:
        print(
            json.dumps(
                {"hardware": asdict(hardware), "recommendation": asdict(recommendation)},
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        console.print(hardware_view(hardware, recommendation))
    return EXIT_OK


async def _cmd_check(app: JarvisApp, args: argparse.Namespace, console: Console) -> int:
    from jarvis.core.setup_checks import run_checks

    await app.startup_probes()
    table = Table(title=f"JARVIS {__version__} – Check", title_justify="left")
    table.add_column("Bereich")
    table.add_column("Status")
    ok = True
    for item in run_checks(app):
        mark = "[green]✔[/]" if item.ok else ("[dim]○[/]" if item.optional else "[yellow]![/]")
        table.add_row(
            f"{mark} {escape(item.title)}",
            escape(item.detail)
            + (f"\n[dim]{escape(item.hint)}[/]" if not item.ok and item.hint else ""),
        )
        if not item.ok and not item.optional:
            ok = False
    console.print(table)
    ready = [m for m in app.router.models() if app.router.model_unavailable_reason(m) is None]
    console.print(f"Bereite Modelle: {', '.join(m.label for m in ready) or 'keine'}")
    console.print(f"Datenordner: {app.data_dir}  ·  Log: {app.log_file}")
    if args.live:
        for model in ready:
            try:
                response = await app.router.complete(
                    [
                        __import__("jarvis.router.base", fromlist=["ChatMessage"]).ChatMessage(
                            role="user", content="Antworte nur mit: OK"
                        )
                    ],
                    pinned_model=model.id,
                    task=TaskKind.SIMPLE,
                )
                console.print(
                    f"[green]✔ {model.label}[/] antwortet ({response.latency_ms} ms): {escape(response.text.strip()[:40])}"
                )
            except Exception as exc:
                console.print(f"[red]✘ {model.label}: {escape(str(exc))}[/]")
                ok = False
    if not ready:
        ok = False
        console.print(
            "[yellow]Noch kein KI-Modell bereit – 'jarvis setup' hilft bei der Einrichtung.[/]"
        )
    console.print(
        "[green]Alles bereit.[/]" if ok else "[yellow]Es gibt offene Punkte (siehe oben).[/]"
    )
    return EXIT_OK if ok else EXIT_ERROR


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
            table.add_row(
                name,
                {"env": "Umgebung/.env", "keyring": "Credential Manager"}.get(
                    source or "", "[dim]nicht gesetzt[/]"
                ),
            )
        console.print(table)
        return EXIT_OK
    name = args.name.strip().upper()
    if not _SECRET_NAME.match(name):
        err.print("[red]Ungültiger Name – erlaubt sind Großbuchstaben, Ziffern und _.[/]")
        return EXIT_ERROR
    try:
        if action == "set":
            value = getpass.getpass(
                f"Wert für {name} (Einfügen mit Strg+V oder Rechtsklick, unsichtbar): "
            )
            store.set(name, value)
            console.print(f"[green]{name} im Windows Credential Manager gespeichert.[/]")
            if store.lookup(name)[1] == "env":
                console.print(
                    f"[yellow]Hinweis: {name} steht auch in .env bzw. der Umgebung und hat dort Vorrang.[/]"
                )
        elif action == "delete":
            console.print(
                f"[green]{name} gelöscht.[/]"
                if store.delete(name)
                else f"{name} war nicht gespeichert."
            )
    except SecretStoreError as exc:
        err.print(f"[red]{escape(str(exc))}[/]")
        return EXIT_ERROR
    except (KeyboardInterrupt, EOFError):
        err.print("[yellow]Abgebrochen – nichts gespeichert.[/]")
        return EXIT_INTERRUPTED
    return EXIT_OK


def _cmd_google(
    loaded: LoadedConfig, args: argparse.Namespace, console: Console, err: Console
) -> int:
    from jarvis.integrations.google import GoogleAccounts, GoogleNotConfigured

    google = GoogleAccounts(
        loaded.data_dir() / "google", calendar_account=loaded.config.google.calendar_account
    )
    action = args.google_command
    if action == "list":
        accounts = google.accounts()
        console.print("\n".join(accounts) if accounts else "Kein Konto verbunden.")
        return EXIT_OK
    if action == "remove":
        console.print("Getrennt." if google.remove_account(args.email) else "Konto nicht gefunden.")
        return EXIT_OK
    if not google.configured():
        err.print(
            f"[yellow]Zuerst den OAuth-Client einrichten (README → Gmail & Kalender) und als\n"
            f"{google.client_file}\nspeichern.[/]"
        )
        return EXIT_ERROR
    try:
        email = google.add_account()
    except GoogleNotConfigured as exc:
        err.print(f"[red]{escape(str(exc))}[/]")
        return EXIT_ERROR
    except Exception as exc:
        err.print(f"[red]Anmeldung fehlgeschlagen: {escape(str(exc))}[/]")
        return EXIT_ERROR
    console.print(
        f"[green]{email} ist verbunden.[/] Weitere Konten: 'jarvis google add' erneut ausführen."
    )
    return EXIT_OK
