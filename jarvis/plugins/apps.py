"""Programme und Webseiten öffnen, laufende Programme anzeigen und schließen."""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

import psutil

from jarvis.plugins.base import ToolContext, ToolResult, obj, tool
from jarvis.security.permissions import Level

log = logging.getLogger(__name__)

#: Häufige Namen → Windows-Befehle.
ALIASES = {
    "editor": "notepad.exe",
    "notepad": "notepad.exe",
    "rechner": "calc.exe",
    "taschenrechner": "calc.exe",
    "calculator": "calc.exe",
    "explorer": "explorer.exe",
    "datei-explorer": "explorer.exe",
    "paint": "mspaint.exe",
    "einstellungen": "ms-settings:",
    "settings": "ms-settings:",
    "terminal": "wt.exe",
    "task-manager": "taskmgr.exe",
    "taskmanager": "taskmgr.exe",
    "systemsteuerung": "control.exe",
    "snipping tool": "ms-screenclip:",
    "kamera": "microsoft.windows.camera:",
}

_START_APPS_CACHE: tuple[float, list[dict[str, str]]] = (0.0, [])


def _creationflags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def start_apps() -> list[dict[str, str]]:
    """Alle Apps aus dem Startmenü (klassisch und Store) via ``Get-StartApps``."""
    global _START_APPS_CACHE
    if sys.platform != "win32":
        return []
    stamp, cached = _START_APPS_CACHE
    if cached and time.monotonic() - stamp < 600:
        return cached
    try:
        output = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-StartApps | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            # utf-8-sig: ein vorangestelltes BOM würde json.loads sonst scheitern lassen.
            encoding="utf-8-sig",
            errors="replace",
            timeout=20,
            creationflags=_creationflags(),
            check=False,
        ).stdout
        data = json.loads(output or "[]")
        apps = [
            {"name": str(d.get("Name", "")), "id": str(d.get("AppID", ""))}
            for d in (data if isinstance(data, list) else [data])
        ]
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        log.debug("Get-StartApps fehlgeschlagen", exc_info=True)
        apps = []
    _START_APPS_CACHE = (time.monotonic(), apps)
    return apps


def find_app(name: str, apps: list[dict[str, str]]) -> dict[str, str] | None:
    wanted = name.lower().strip()
    if not apps:
        return None
    exact = [a for a in apps if a["name"].lower() == wanted]
    if exact:
        return exact[0]
    contains = [a for a in apps if wanted in a["name"].lower()]
    if contains:
        return sorted(contains, key=lambda a: len(a["name"]))[0]
    names = [a["name"].lower() for a in apps]
    close = difflib.get_close_matches(wanted, names, n=1, cutoff=0.6)
    if close:
        return apps[names.index(close[0])]
    return None


def launch(target: str) -> None:
    if sys.platform == "win32":
        os.startfile(target)  # type: ignore[attr-defined]  # nur unter Windows vorhanden
    elif sys.platform == "darwin":
        subprocess.Popen(["open", target])
    else:
        subprocess.Popen(["xdg-open", target])


@tool(
    name="app_open",
    description=(
        "Open a program, file or folder on the PC. Accepts app names as shown in the Start menu "
        "(e.g. 'Spotify', 'Word', 'Rechner', 'Edge'), full paths, or folders."
    ),
    parameters=obj({"name": {"type": "string"}}, ["name"]),
    category="programme",
)
async def app_open(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    name = str(args["name"]).strip()
    path = ctx.app.paths.resolve(name) if any(c in name for c in "\\/:") else None
    if path is not None and path.exists():
        if ctx.app.paths.is_secret(path):
            return ToolResult.error("Diese Datei enthält Zugangsdaten und wird nicht geöffnet.")
        await asyncio.to_thread(launch, str(path))
        return ToolResult(f"Geöffnet: {path}")
    alias = ALIASES.get(name.lower())
    if alias:
        await asyncio.to_thread(launch, alias)
        return ToolResult(f"{name} wird geöffnet.")
    app = find_app(name, await asyncio.to_thread(start_apps))
    if app is not None:
        await asyncio.to_thread(launch, f"shell:AppsFolder\\{app['id']}")
        return ToolResult(f"{app['name']} wird geöffnet.")
    if sys.platform == "win32" and not Path(name).suffix:
        try:
            await asyncio.to_thread(launch, name)
            return ToolResult(f"{name} wird gestartet.")
        except OSError:
            pass
    return ToolResult.error(
        f"Programm '{name}' nicht gefunden. Tipp: genauer Name wie im Startmenü."
    )


@tool(
    name="url_open",
    description="Open a web page in the default browser.",
    parameters=obj({"url": {"type": "string"}}, ["url"]),
    category="programme",
)
async def url_open(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    url = str(args["url"]).strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    await asyncio.to_thread(webbrowser.open, url)
    return ToolResult(f"Geöffnet: {url}")


@tool(
    name="app_list_running",
    description="List running programs (with memory use).",
    parameters=obj({}),
    category="programme",
)
async def app_list_running(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    from jarvis.plugins.system_tools import top_processes

    processes = await asyncio.to_thread(top_processes, 30)
    return ToolResult("\n".join(f"{p['name']}: {p['ram_mb']} MB" for p in processes))


def _matching_processes(name: str) -> list[psutil.Process]:
    wanted = name.lower().removesuffix(".exe")
    result = []
    for proc in psutil.process_iter(["name", "pid"]):
        try:
            proc_name = (proc.info["name"] or "").lower().removesuffix(".exe")
        except psutil.Error:
            continue
        if proc_name == wanted and proc.pid != os.getpid():
            result.append(proc)
    return result


@tool(
    name="app_close",
    description="Close a running program by its process name (e.g. 'notepad', 'chrome'). Unsaved work may be lost.",
    parameters=obj({"name": {"type": "string"}}, ["name"]),
    level=Level.CONFIRM,
    category="programme",
    summary=lambda a: (
        f"Programm schließen: {a.get('name')} (ungespeicherte Daten gehen evtl. verloren)"
    ),
)
async def app_close(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    name = str(args["name"])
    if name.lower().removesuffix(".exe") in {
        "explorer",
        "csrss",
        "winlogon",
        "lsass",
        "svchost",
        "system",
        "python",
        "pythonw",
    }:
        return ToolResult.error(f"{name} ist ein Systemprozess und wird nicht beendet.")

    def close() -> int:
        procs = _matching_processes(name)
        for proc in procs:
            try:
                proc.terminate()
            except psutil.Error:
                continue
        _, alive = psutil.wait_procs(procs, timeout=5)
        for proc in alive:
            try:
                proc.kill()
            except psutil.Error:
                continue
        return len(procs)

    count = await asyncio.to_thread(close)
    if count == 0:
        return ToolResult.error(f"Kein laufendes Programm namens '{name}' gefunden.")
    return ToolResult(f"{name} beendet ({count} Prozess{'e' if count != 1 else ''}).")
