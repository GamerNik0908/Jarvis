"""Geführte Einrichtung (``jarvis setup``) – alles kostenlos.

Schritte: Name & Wohnort → Claude Code (Pro-Abo) → Gemini-Key → Ollama und
lokale Modelle → Google (Gmail/Kalender) → Autostart.
"""

from __future__ import annotations

import asyncio
import getpass
import shutil
import subprocess
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from jarvis.core import autostart
from jarvis.core.app import JarvisApp
from jarvis.core.config import PROJECT_ROOT, update_local_config
from jarvis.core.setup_checks import missing_ollama_models, run_checks
from jarvis.router.providers.claude_code_provider import ClaudeCodeProvider
from jarvis.router.providers.ollama_provider import OllamaProvider


async def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = await asyncio.to_thread(input, f"{prompt}{suffix}: ")
    return answer.strip() or default


async def _yes(prompt: str, default: bool = True) -> bool:
    answer = (await _ask(f"{prompt} ({'J/n' if default else 'j/N'})")).lower()
    if not answer:
        return default
    return answer in {"j", "ja", "y", "yes"}


def _winget(package: str, console: Console) -> bool:
    if shutil.which("winget") is None:
        console.print("[yellow]winget nicht gefunden – bitte manuell installieren.[/]")
        return False
    result = subprocess.run(
        [
            "winget",
            "install",
            "-e",
            "--id",
            package,
            "--accept-package-agreements",
            "--accept-source-agreements",
        ],
        check=False,
    )
    return result.returncode == 0


async def run_setup(app: JarvisApp, console: Console) -> int:
    console.print(
        Panel.fit(
            "[bold cyan]JARVIS-Einrichtung[/]\nAlles hier ist kostenlos. Du kannst jeden Schritt überspringen (Enter).",
            border_style="cyan",
        )
    )
    await app.startup_probes()
    changes: dict[str, Any] = {}

    # 1) Persönliches
    name = await _ask("Wie soll JARVIS dich nennen?", app.config.assistant.user_name)
    location = await _ask("Dein Wohnort (für Wetter)", app.config.assistant.home_location)
    changes["assistant"] = {"user_name": name, "home_location": location}

    # 2) Claude Code
    claude = app.provider_of_type(ClaudeCodeProvider)
    if claude is not None:
        reason = await claude.probe()
        if reason and "nicht installiert" in reason:
            console.print(
                "\n[bold]Claude Code[/] fehlt. Damit nutzt JARVIS dein Claude-Pro-Abo (ohne API-Kosten) – auch zum Selbstprogrammieren."
            )
            if await _yes("Jetzt mit winget installieren?"):
                _winget("Anthropic.ClaudeCode", console)
                console.print(
                    "Öffne danach ein [bold]neues[/] Terminal, falls 'claude' nicht gefunden wird."
                )
                claude.cli._executable = None
                reason = await claude.probe()
        if reason and ("angemeldet" in reason or "API-Key" in reason):
            console.print(
                "\n[bold]Claude-Anmeldung:[/] Öffne ein zweites Terminal, tippe [bold]claude[/], melde dich mit deinem "
                "Pro-Konto an (Browser) und beende Claude dann mit /exit."
            )
            await _ask("Enter drücken, wenn erledigt")
            reason = await claude.probe()
        console.print(
            "[green]✔ Claude über Pro-Abo bereit[/]"
            if not reason
            else f"[yellow]Claude: {escape(reason)}[/]"
        )

    # 3) Gemini
    if not app.secrets.get("GEMINI_API_KEY"):
        console.print(
            "\n[bold]Gemini (kostenlos):[/] Erstelle einen Key unter https://aistudio.google.com/apikey "
            "(Google-Konto genügt, keine Kreditkarte)."
        )
        key = await asyncio.to_thread(
            getpass.getpass, "Key einfügen (unsichtbar, Enter = überspringen): "
        )
        if key.strip():
            app.secrets.set("GEMINI_API_KEY", key.strip())
            console.print("[green]✔ Im Windows Credential Manager gespeichert[/]")

    # 4) Ollama
    ollama = app.provider_of_type(OllamaProvider)
    if ollama is not None:
        await ollama.probe()
        if ollama.unavailable_reason():
            console.print(
                "\n[bold]Ollama[/] (lokale KI, offline & privat) ist nicht installiert oder läuft nicht."
            )
            if await _yes("Jetzt mit winget installieren?", default=False):
                _winget("Ollama.Ollama", console)
                await asyncio.sleep(5)
                await ollama.probe()
        missing = missing_ollama_models(app)
        if not ollama.unavailable_reason() and missing:
            console.print(
                f"\nEmpfohlene lokale Modelle für deinen PC: [bold]{', '.join(missing)}[/] (je 1–20 GB Download)"
            )
            if await _yes("Jetzt herunterladen?"):
                for model in missing:
                    console.print(f"Lade {model} …")
                    last = -10
                    async for progress in ollama.pull(model):
                        total, done = progress.get("total") or 0, progress.get("completed") or 0
                        if total:
                            percent = int(done * 100 / total)
                            if percent >= last + 10:
                                console.print(f"  {percent} %")
                                last = percent
                    console.print(f"[green]✔ {model}[/]")

    # 5) Google
    console.print(
        "\n[bold]Gmail & Kalender[/] (optional): Anleitung im README, Abschnitt 'Gmail & Kalender'. "
        f"OAuth-Datei: {app.google.client_file}"
    )
    if app.google.configured():
        while await _yes(
            f"Google-Konto verbinden? (verbunden: {', '.join(app.google.accounts()) or 'keins'})",
            default=not app.google.accounts(),
        ):
            try:
                email = await asyncio.to_thread(app.google.add_account)
                console.print(f"[green]✔ {email} verbunden[/]")
            except Exception as exc:
                console.print(f"[red]{escape(str(exc))}[/]")
                break

    # 6) Autostart
    if not autostart.is_enabled() and await _yes(
        "\nJARVIS bei jeder Windows-Anmeldung automatisch starten?"
    ):
        try:
            autostart.enable(PROJECT_ROOT)
            console.print("[green]✔ Autostart aktiv[/]")
        except RuntimeError as exc:
            console.print(f"[yellow]{escape(str(exc))}[/]")

    update_local_config(app.loaded, changes)
    console.print("\n[bold]Zusammenfassung[/]")
    app.router.unblock()
    await app.startup_probes()
    for item in run_checks(app):
        mark = "[green]✔[/]" if item.ok else ("[dim]○[/]" if item.optional else "[yellow]![/]")
        console.print(f"{mark} {escape(item.title)}: {escape(item.detail)}")
    console.print("\nFertig! Starte JARVIS mit [bold]jarvis[/] (bzw. .\\scripts\\jarvis.cmd).")
    return 0
