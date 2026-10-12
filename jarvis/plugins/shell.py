"""PowerShell-Befehle ausführen – immer nur nach deiner Bestätigung."""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import subprocess
from typing import Any

from jarvis.core.tasks import kill_process_tree
from jarvis.plugins.base import ToolContext, ToolResult, obj, tool
from jarvis.security.permissions import Level

#: Befehle, die nie ausgeführt werden – egal, ob du zustimmst.
FORBIDDEN_PATTERNS = [
    r"\bformat(-volume)?\b",
    r"\bdiskpart\b",
    r"\bbcdedit\b",
    r"\bcipher\s+/w",
    r"remove-item\s+.*-recurse.*\b[a-z]:\\?\s*$",
    r"\brd\s+/s\s+/q\s+[a-z]:\\?\s*$",
    r"reg(\.exe)?\s+delete\s+hklm",
    r"\bvssadmin\s+delete",
    r"set-executionpolicy\s+unrestricted\s+.*localmachine",
    r"invoke-expression.*(downloadstring|iwr|invoke-webrequest)",
    r"\biex\b.*\b(iwr|irm|invoke-restmethod|invoke-webrequest)\b",
]


def shell_level(args: dict[str, Any], ctx: ToolContext) -> Level:
    command = str(args.get("command", "")).lower()
    if any(re.search(pattern, command) for pattern in FORBIDDEN_PATTERNS):
        return Level.FORBIDDEN
    return Level.CONFIRM


def _powershell() -> str | None:
    return shutil.which("powershell.exe") or shutil.which("pwsh") or shutil.which("powershell")


@tool(
    name="powershell_run",
    description=(
        "Run a PowerShell command on the PC and return its output. Use for tasks no other "
        "tool covers. The user must confirm every command."
    ),
    parameters=obj(
        {
            "command": {"type": "string"},
            "timeout_s": {"type": "integer", "description": "Default 60, max 600"},
        },
        ["command"],
    ),
    level=shell_level,
    category="system",
    summary=lambda a: f"PowerShell ausführen:\n{a.get('command')}",
    timeout_s=620,
)
async def powershell_run(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    exe = _powershell()
    if exe is None:
        return ToolResult.error("PowerShell nicht gefunden")
    timeout = min(int(args.get("timeout_s") or 60), 600)
    command = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + str(args["command"])
    proc = await asyncio.create_subprocess_exec(
        exe,
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        command,
        # Keine geerbte Eingabe – PowerShell wartet sonst ggf. auf eine offene stdin-Pipe.
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(ctx.app.workspace),
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        # Den ganzen Prozessbaum beenden – unter Windows liefen von PowerShell gestartete
        # Programme sonst weiter.
        kill_process_tree(proc.pid)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), 5)
        return ToolResult.error(f"Abgebrochen nach {timeout} s")
    # utf-8-sig: verträgt ein BOM am Anfang, falls PowerShell eines voranstellt.
    text = out.decode("utf-8-sig", "replace").strip()
    error = err.decode("utf-8-sig", "replace").strip()
    body = text or "(keine Ausgabe)"
    if error:
        body += f"\n[Fehlerausgabe]\n{error}"
    return ToolResult(
        f"Exit-Code {proc.returncode}\n{body}", ok=proc.returncode == 0, sensitive=True
    )
