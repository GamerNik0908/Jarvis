"""Claude Code (CLI) als kostenloser Zugang zu Claude über dein Claude-Pro-Abo.

JARVIS startet ``claude -p`` (nicht-interaktiver Modus) und liest die
Ausgabe als ``stream-json``. Wichtig für "keine Kosten":

* ``--bare`` wird bewusst NICHT verwendet – Bare-Mode akzeptiert nur
  kostenpflichtige API-Keys, nicht den Abo-Login.
* ``ANTHROPIC_API_KEY`` und ``ANTHROPIC_AUTH_TOKEN`` werden aus der Umgebung
  des Kindprozesses entfernt, damit Claude Code garantiert dein Abo nutzt und
  nicht versehentlich über einen API-Key abrechnet.

Das Abo hat Nutzungslimits (5-Stunden-Fenster und Wochenlimit, geteilt mit
claude.ai). Ist ein Limit erreicht, meldet der Runner das mit der Wartezeit,
damit der Router solange auf andere Modelle ausweicht.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from jarvis.core.tasks import kill_process_tree

log = logging.getLogger(__name__)

#: Ab dieser Version kennt Claude Code ``--permission-prompts none``.
_PERMISSION_PROMPTS_MIN = (2, 1, 259)
_STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
_DEFAULT_LIMIT_PAUSE_S = 30 * 60
_STREAM_LIMIT = 32 * 1024 * 1024

EventKind = Literal[
    "init", "text", "tool_use", "tool_result", "status", "assistant_error", "result", "other"
]


@dataclass
class CCResult:
    """Abschluss eines Claude-Code-Laufs."""

    is_error: bool
    text: str
    session_id: str | None = None
    error_kind: str | None = None
    api_status: int | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    model: str | None = None
    num_turns: int = 0
    duration_ms: int = 0
    permission_denials: list[Any] = field(default_factory=list)
    cost_estimate_usd: float = 0.0
    subtype: str | None = None


@dataclass
class CCEvent:
    kind: EventKind
    text: str = ""
    tool_name: str | None = None
    tool_input: Mapping[str, Any] | None = None
    #: Nur bei ``kind == "result"``.
    result: CCResult | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


class ClaudeCodeError(Exception):
    """Claude Code konnte nicht gestartet werden oder ist abgebrochen."""

    def __init__(self, message: str, *, kind: str = "unknown", retry_after_s: float = 0) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_after_s = retry_after_s


@dataclass
class ClaudeCodeRun:
    """Parameter eines ``claude -p``-Aufrufs."""

    prompt: str
    cwd: Path
    model: str | None = None
    #: Ersetzt den Standard-System-Prompt von Claude Code komplett (Chat-Modus).
    system_prompt: str | None = None
    #: Ergänzt den Standard-System-Prompt (Programmier-Modus).
    append_system_prompt: str | None = None
    #: ``[]`` = alle eingebauten Werkzeuge aus; ``None`` = Standard-Werkzeuge.
    tools: Sequence[str] | None = ()
    allowed_tools: Sequence[str] = ()
    disallowed_tools: Sequence[str] = ()
    permission_mode: str | None = None
    mcp_config: Path | None = None
    max_turns: int | None = None
    resume_session: str | None = None
    keep_session: bool = False
    effort: str | None = None
    timeout_s: float = 600
    extra_env: Mapping[str, str] = field(default_factory=dict)
    add_dirs: Sequence[Path] = ()


def find_claude_executable(configured: str | None = None) -> str | None:
    """Sucht ``claude`` im PATH und an den typischen Windows-Installationsorten."""
    candidates: list[str] = []
    if configured:
        candidates.append(configured)
    found = shutil.which("claude")
    if found:
        candidates.append(found)
    home = Path.home()
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        candidates += [
            str(home / ".local" / "bin" / "claude.exe"),
            str(local / "Microsoft" / "WinGet" / "Links" / "claude.exe"),
            str(roaming / "npm" / "claude.cmd"),
        ]
    else:
        candidates.append(str(home / ".local" / "bin" / "claude"))
    for candidate in candidates:
        path = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
        if path:
            return path
    return None


def child_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Umgebung für Claude Code – ohne API-Keys, damit das Abo genutzt wird."""
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV}
    env.setdefault("MCP_TOOL_TIMEOUT", str(15 * 60 * 1000))  # Bestätigungen dürfen dauern
    env.setdefault("CLAUDE_CODE_DISABLE_TERMINAL_TITLE", "1")
    if extra:
        env.update(extra)
    return env


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def parse_version(text: str) -> tuple[int, ...] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(g) for g in match.groups()) if match else None


class ClaudeCodeCLI:
    """Kapselt Aufrufe von ``claude`` (Version, Login-Status, Läufe)."""

    def __init__(self, executable: str | None = None) -> None:
        self._configured = executable
        self._executable: str | None = None
        self._version: tuple[int, ...] | None = None

    @property
    def executable(self) -> str | None:
        if self._executable is None:
            self._executable = find_claude_executable(self._configured)
        return self._executable

    async def _run_simple(self, *args: str, limit_s: float = 20) -> tuple[int, str, str]:
        exe = self.executable
        if exe is None:
            raise ClaudeCodeError("Claude Code ist nicht installiert", kind="not_installed")
        proc = await asyncio.create_subprocess_exec(
            exe,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env(),
            creationflags=_creation_flags(),
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), limit_s)
        except TimeoutError:
            kill_process_tree(proc.pid)
            raise ClaudeCodeError("Claude Code antwortet nicht", kind="timeout") from None
        return (
            proc.returncode or 0,
            out.decode("utf-8", "replace"),
            err.decode("utf-8", "replace"),
        )

    async def version(self) -> tuple[int, ...] | None:
        if self._version is None:
            code, out, _ = await self._run_simple("--version")
            self._version = parse_version(out) if code == 0 else None
        return self._version

    async def auth_status(self) -> dict[str, Any]:
        """Login-Status als Dictionary (``loggedIn``, ``authMethod`` …)."""
        code, out, _ = await self._run_simple("auth", "status")
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            data = {"loggedIn": code == 0}
        return data if isinstance(data, dict) else {"loggedIn": False}

    async def build_args(self, run: ClaudeCodeRun, files: _TempFiles) -> list[str]:
        exe = self.executable
        if exe is None:
            raise ClaudeCodeError("Claude Code ist nicht installiert", kind="not_installed")
        args = [exe, "-p", "--output-format", "stream-json", "--verbose"]
        args.append("--include-partial-messages")
        if run.model:
            args += ["--model", run.model]
        if run.effort:
            args += ["--effort", run.effort]
        if run.system_prompt is not None:
            args += ["--system-prompt-file", str(files.write("system", run.system_prompt))]
        if run.append_system_prompt:
            args += [
                "--append-system-prompt-file",
                str(files.write("append", run.append_system_prompt)),
            ]
        if run.tools is not None:
            args += ["--tools", ",".join(run.tools)]
        if run.allowed_tools:
            args += ["--allowedTools", ",".join(run.allowed_tools)]
        if run.disallowed_tools:
            args += ["--disallowedTools", ",".join(run.disallowed_tools)]
        if run.permission_mode:
            args += ["--permission-mode", run.permission_mode]
        version = await self.version()
        if version is not None and version >= _PERMISSION_PROMPTS_MIN:
            args += ["--permission-prompts", "none"]
        if run.mcp_config is not None:
            args += ["--mcp-config", str(run.mcp_config)]
        args.append("--strict-mcp-config")  # nur JARVIS-eigene MCP-Server, nichts Fremdes
        if run.max_turns:
            args += ["--max-turns", str(run.max_turns)]
        if run.resume_session:
            args += ["--resume", run.resume_session]
        elif not run.keep_session:
            args.append("--no-session-persistence")
        for directory in run.add_dirs:
            args += ["--add-dir", str(directory)]
        return args

    async def stream(self, run: ClaudeCodeRun) -> AsyncGenerator[CCEvent, None]:
        """Startet Claude Code und liefert Ereignisse; das letzte hat ``kind == "result"``."""
        run.cwd.mkdir(parents=True, exist_ok=True)
        # Temporäre Dateien NICHT im Arbeitsordner – dort würden sie bei der
        # Selbstverbesserung im Git-Diff landen.
        with _TempFiles(Path(tempfile.gettempdir()) / "jarvis-claude") as files:
            args = await self.build_args(run, files)
            log.debug("Starte Claude Code: %s", " ".join([*args[:3], "…"]))
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    cwd=str(run.cwd),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=child_env(run.extra_env),
                    limit=_STREAM_LIMIT,
                    creationflags=_creation_flags(),
                )
            except OSError as exc:
                raise ClaudeCodeError(
                    f"Claude Code konnte nicht gestartet werden: {exc}", kind="not_installed"
                ) from exc
            stderr_task = asyncio.create_task(_drain(proc.stderr))
            try:
                assert proc.stdin is not None and proc.stdout is not None
                proc.stdin.write(run.prompt.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
                deadline = time.monotonic() + run.timeout_s
                saw_text = False
                result: CCResult | None = None
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ClaudeCodeError("Zeitüberschreitung bei Claude Code", kind="timeout")
                    try:
                        line = await asyncio.wait_for(proc.stdout.readline(), remaining)
                    except TimeoutError:
                        raise ClaudeCodeError(
                            "Zeitüberschreitung bei Claude Code", kind="timeout"
                        ) from None
                    if not line:
                        break
                    for event in parse_line(line):
                        if event.kind == "text":
                            saw_text = True
                        if event.kind == "result" and event.result is not None:
                            result = event.result
                            if not saw_text and result.text and not result.is_error:
                                yield CCEvent("text", text=result.text)
                        yield event
                await proc.wait()
                if result is None:
                    stderr = (await stderr_task).strip()
                    raise ClaudeCodeError(
                        "Claude Code wurde ohne Ergebnis beendet"
                        + (f": {stderr[-400:]}" if stderr else ""),
                        kind="crashed",
                    )
            finally:
                if proc.returncode is None:
                    await _terminate(proc)
                if not stderr_task.done():
                    stderr_task.cancel()


class _TempFiles:
    """Temporäre Dateien für lange Argumente (System-Prompt, MCP-Konfiguration)."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._paths: list[Path] = []

    def __enter__(self) -> _TempFiles:
        self._dir.mkdir(parents=True, exist_ok=True)
        return self

    def write(self, name: str, content: str) -> Path:
        path = self._dir / f"{name}-{os.getpid()}-{time.monotonic_ns()}.txt"
        path.write_text(content, encoding="utf-8")
        self._paths.append(path)
        return path

    def __exit__(self, *exc: object) -> None:
        for path in self._paths:
            with contextlib.suppress(OSError):
                path.unlink()


async def _drain(stream: asyncio.StreamReader | None) -> str:
    if stream is None:
        return ""
    data = await stream.read()
    return data.decode("utf-8", "replace")


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    try:
        if sys.platform == "win32":
            # terminate() ist unter Windows ohnehin hart und beendet keine Kindprozesse –
            # node.exe hinter claude.cmd oder die MCP-Brücke liefen sonst weiter.
            kill_process_tree(proc.pid)
        else:
            proc.terminate()
        await asyncio.wait_for(proc.wait(), 5)
    except (ProcessLookupError, TimeoutError):
        kill_process_tree(proc.pid)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    except Exception:  # pragma: no cover - Aufräumen darf nie selbst fehlschlagen
        log.debug("Beenden von Claude Code fehlgeschlagen", exc_info=True)


def parse_line(line: bytes | str) -> list[CCEvent]:
    """Übersetzt eine stream-json-Zeile in null oder mehr :class:`CCEvent`."""
    text = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [CCEvent("other", text=text)]
    if not isinstance(data, dict):
        return []
    kind = data.get("type")
    main_thread = data.get("parent_tool_use_id") is None
    if kind == "system":
        subtype = data.get("subtype")
        if subtype == "init":
            return [CCEvent("init", raw=data)]
        if subtype == "api_retry":
            return [
                CCEvent(
                    "status",
                    text=f"Claude: neuer Versuch {data.get('attempt')}/{data.get('max_retries')}",
                    raw=data,
                )
            ]
        return [CCEvent("other", raw=data)]
    if kind == "stream_event":
        event = data.get("event") or {}
        delta = event.get("delta") or {}
        if (
            main_thread
            and event.get("type") == "content_block_delta"
            and delta.get("type") == "text_delta"
            and delta.get("text")
        ):
            return [CCEvent("text", text=str(delta["text"]), raw=data)]
        return []
    if kind == "assistant":
        message = data.get("message") or {}
        if data.get("error") or data.get("is_api_error_message"):
            texts = [
                b.get("text", "") for b in message.get("content", []) if b.get("type") == "text"
            ]
            return [
                CCEvent(
                    "assistant_error",
                    text=" ".join(t for t in texts if t) or str(data.get("error")),
                    raw=data,
                )
            ]
        events = []
        for block in message.get("content", []) or []:
            if block.get("type") == "tool_use":
                events.append(
                    CCEvent(
                        "tool_use",
                        tool_name=str(block.get("name")),
                        tool_input=block.get("input") or {},
                        raw=data,
                    )
                )
        return events
    if kind == "user":
        message = data.get("message") or {}
        events = []
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    events.append(CCEvent("tool_result", text=_tool_result_text(block), raw=data))
        return events
    if kind == "result":
        return [CCEvent("result", text=str(data.get("result") or ""), result=_parse_result(data))]
    return [CCEvent("other", raw=data)]


def _tool_result_text(block: Mapping[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return ""


def _parse_result(data: Mapping[str, Any]) -> CCResult:
    model_usage = data.get("modelUsage") or {}
    model = next(iter(model_usage), None) if isinstance(model_usage, dict) else None
    text = str(data.get("result") or "")
    is_error = bool(data.get("is_error")) or str(data.get("subtype", "")).startswith("error")
    error_kind = None
    if data.get("subtype") == "error_max_turns":
        # Claude Code liefert hier keinen Ergebnistext – nur den Subtyp.
        error_kind = "max_turns"
    elif is_error:
        error_kind = classify_error_text(text, data.get("api_error_status"))
    return CCResult(
        is_error=is_error,
        text=text,
        session_id=data.get("session_id"),
        error_kind=error_kind,
        api_status=data.get("api_error_status"),
        usage=data.get("usage") or {},
        model=model,
        num_turns=int(data.get("num_turns") or 0),
        duration_ms=int(data.get("duration_ms") or 0),
        permission_denials=list(data.get("permission_denials") or []),
        cost_estimate_usd=float(data.get("total_cost_usd") or 0.0),
        subtype=data.get("subtype"),
    )


def classify_error_text(text: str, status: object = None) -> str:
    """Ordnet eine Fehlermeldung einer Kategorie zu."""
    lowered = text.lower()
    if status == 429 or ("limit" in lowered and ("reset" in lowered or "reached" in lowered)):
        return "rate_limit"
    if status in (401, 403) or "api key" in lowered or "log in" in lowered or "login" in lowered:
        return "authentication_failed"
    if status == 404 or ("model" in lowered and "not found" in lowered):
        return "model_not_found"
    if status == 529 or "overloaded" in lowered:
        return "overloaded"
    if isinstance(status, int) and status >= 500:
        return "server_error"
    if status == 400:
        return "invalid_request"
    if "max turns" in lowered or "maximum number of turns" in lowered:
        return "max_turns"
    return "unknown"


_RESET_PATTERN = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.I)


def limit_wait_seconds(text: str, *, now: datetime | None = None) -> float:
    """Wie lange bis zum Zurücksetzen des Abo-Limits? (Standard: 30 Minuten)."""
    now = now or datetime.now()
    epoch = re.search(r"\|(\d{9,11})\b", text)
    if epoch:
        wait = int(epoch.group(1)) - now.timestamp()
        if 0 < wait < 8 * 24 * 3600:
            return wait
    match = _RESET_PATTERN.search(text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        meridiem = (match.group(3) or "").lower()
        if meridiem == "pm" and hour < 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
        if 0 <= hour < 24:
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return (target - now).total_seconds()
    return _DEFAULT_LIMIT_PAUSE_S
