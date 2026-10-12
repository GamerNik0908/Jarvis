"""Tests für Claude Code (CLI-Runner) und das zugehörige Anbieter-Plugin – ohne echtes ``claude``."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from collections.abc import AsyncGenerator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from jarvis.core.config import ModelConfig, ProviderConfig, RouterConfig
from jarvis.core.db import Database
from jarvis.integrations import claude_code
from jarvis.integrations.claude_code import (
    CCEvent,
    ClaudeCodeCLI,
    ClaudeCodeError,
    ClaudeCodeRun,
    _TempFiles,
    child_env,
    classify_error_text,
    find_claude_executable,
    limit_wait_seconds,
    parse_line,
    parse_version,
)
from jarvis.integrations.mcp_bridge import ToolBridge
from jarvis.router.base import ChatMessage, ChatRequest, ToolCall, ToolSpec
from jarvis.router.costs import CostTracker
from jarvis.router.errors import (
    ModelNotFoundError,
    ProviderAuthError,
    ProviderError,
    ProviderRequestError,
    ProviderUnavailableError,
)
from jarvis.router.providers.claude_code_provider import ClaudeCodeProvider, render_transcript
from jarvis.router.router import ModelRouter
from jarvis.security.secrets import SecretStore

NOW = datetime(2026, 10, 12, 14, 30)
SESSION = "8f3c2a1e-5b7d-4c9e-a2f1-0d6b3e4c5a71"


def _line(data: Mapping[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False)


INIT = _line(
    {
        "type": "system",
        "subtype": "init",
        "cwd": "/home/jarvis/.jarvis-claude",
        "session_id": SESSION,
        "tools": [],
        "mcp_servers": [],
        "model": "claude-sonnet-4-5-20250929",
        "permissionMode": "default",
        "apiKeySource": "none",
    }
)


def _delta(text: str, *, parent: str | None = None) -> str:
    return _line(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
            "session_id": SESSION,
            "parent_tool_use_id": parent,
        }
    )


TOOL_USE = _line(
    {
        "type": "assistant",
        "message": {
            "id": "msg_01",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-5-20250929",
            "content": [
                {"type": "text", "text": "Ich schaue nach."},
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "mcp__jarvis__web_search",
                    "input": {"query": "Wetter Berlin"},
                },
            ],
            "stop_reason": "tool_use",
        },
        "parent_tool_use_id": None,
        "session_id": SESSION,
    }
)

TOOL_RESULT = _line(
    {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "tool_use_id": "toolu_01",
                    "type": "tool_result",
                    "content": [
                        {"type": "text", "text": "Sonnig,"},
                        {"type": "text", "text": "21 °C"},
                    ],
                }
            ],
        },
        "parent_tool_use_id": None,
        "session_id": SESSION,
    }
)


def _result(**overrides: Any) -> str:
    data: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 2345,
        "duration_api_ms": 2001,
        "num_turns": 1,
        "result": "Hallo Welt",
        "session_id": SESSION,
        "total_cost_usd": 0.0421,
        "usage": {
            "input_tokens": 12,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 2000,
            "output_tokens": 34,
        },
        "modelUsage": {"claude-sonnet-4-5-20250929": {"inputTokens": 12, "outputTokens": 34}},
        "permission_denials": [],
    }
    data.update(overrides)
    return _line(data)


# ---------------------------------------------------------------- parse_line


def test_parse_init_keeps_session_id() -> None:
    [event] = parse_line(INIT.encode("utf-8"))
    assert event.kind == "init"
    assert event.raw["session_id"] == SESSION


def test_parse_text_delta() -> None:
    [event] = parse_line(_delta("Hallo"))
    assert event.kind == "text"
    assert event.text == "Hallo"


def test_parse_ignores_subagent_and_non_text_deltas() -> None:
    assert parse_line(_delta("intern", parent="toolu_01")) == []
    assert parse_line(_delta("")) == []
    json_delta = _line(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "input_json_delta", "partial_json": '{"q'},
            },
            "parent_tool_use_id": None,
        }
    )
    assert parse_line(json_delta) == []
    start = _line({"type": "stream_event", "event": {"type": "message_start"}})
    assert parse_line(start) == []


def test_parse_assistant_tool_use() -> None:
    # Der Text des Assistant-Blocks kam schon als stream_event – nur der Tool-Aufruf zählt.
    [event] = parse_line(TOOL_USE)
    assert event.kind == "tool_use"
    assert event.tool_name == "mcp__jarvis__web_search"
    assert event.tool_input == {"query": "Wetter Berlin"}


def test_parse_assistant_api_error() -> None:
    line = _line(
        {
            "type": "assistant",
            "message": {
                "model": "<synthetic>",
                "role": "assistant",
                "content": [{"type": "text", "text": "Claude AI usage limit reached|1760000000"}],
            },
            "error": "rate_limit",
            "parent_tool_use_id": None,
        }
    )
    [event] = parse_line(line)
    assert event.kind == "assistant_error"
    assert event.text == "Claude AI usage limit reached|1760000000"
    # Ohne Text wird der Fehlercode verwendet.
    [bare] = parse_line(_line({"type": "assistant", "message": {"content": []}, "error": "x"}))
    assert bare.text == "x"


def test_parse_user_tool_result() -> None:
    [event] = parse_line(TOOL_RESULT)
    assert event.kind == "tool_result"
    assert event.text == "Sonnig, 21 °C"
    as_string = _line(
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": "fertig"}]},
        }
    )
    assert [e.text for e in parse_line(as_string)] == ["fertig"]
    # Normale Benutzernachricht (String-Inhalt) erzeugt kein Ereignis.
    assert parse_line(_line({"type": "user", "message": {"content": "Hallo"}})) == []


def test_parse_api_retry_status() -> None:
    line = _line({"type": "system", "subtype": "api_retry", "attempt": 2, "max_retries": 10})
    [event] = parse_line(line)
    assert event.kind == "status"
    assert event.text == "Claude: neuer Versuch 2/10"


def test_parse_result_success() -> None:
    [event] = parse_line(_result())
    assert event.kind == "result"
    assert event.text == "Hallo Welt"
    result = event.result
    assert result is not None
    assert not result.is_error and result.error_kind is None
    assert result.session_id == SESSION
    assert result.model == "claude-sonnet-4-5-20250929"
    assert result.num_turns == 1 and result.duration_ms == 2345
    assert result.usage["cache_read_input_tokens"] == 2000
    assert result.cost_estimate_usd == pytest.approx(0.0421)
    assert result.subtype == "success"


def test_parse_result_error_with_status() -> None:
    [event] = parse_line(
        _result(
            is_error=True,
            api_error_status=429,
            result="API Error: Rate limit",
            modelUsage={},
        )
    )
    result = event.result
    assert result is not None
    assert result.is_error
    assert result.api_status == 429
    assert result.error_kind == "rate_limit"
    assert result.model is None


def test_parse_result_error_subtype_counts_as_error() -> None:
    [event] = parse_line(_result(subtype="error_during_execution", result="overloaded"))
    assert event.result is not None
    assert event.result.is_error
    assert event.result.error_kind == "overloaded"


def test_parse_result_max_turns_without_text() -> None:
    # Echte Ausgabe von Claude Code: kein ``result``-Text, nur der Subtyp.
    line = _line(
        {
            "type": "result",
            "subtype": "error_max_turns",
            "is_error": False,
            "num_turns": 2,
            "session_id": SESSION,
        }
    )
    [event] = parse_line(line)
    assert event.result is not None
    assert event.result.is_error
    assert event.result.error_kind == "max_turns"


def test_parse_line_edge_cases() -> None:
    assert parse_line(b"") == []
    assert parse_line("   \n") == []
    [other] = parse_line("kein json")
    assert other.kind == "other" and other.text == "kein json"
    assert parse_line("[1, 2]") == []
    assert [e.kind for e in parse_line(_line({"type": "rate_limit_event"}))] == ["other"]
    assert [e.kind for e in parse_line(_line({"type": "system", "subtype": "hook"}))] == ["other"]


# ---------------------------------------------------------- Fehler und Limits


@pytest.mark.parametrize(
    ("text", "status", "expected"),
    [
        ("Claude AI usage limit reached|1760000000", None, "rate_limit"),
        ("You've hit your limit · resets 3pm (Europe/Berlin)", None, "rate_limit"),
        ("", 429, "rate_limit"),
        ("Invalid API key · Please run /login", None, "authentication_failed"),
        ("", 401, "authentication_failed"),
        ("", 403, "authentication_failed"),
        ("Model not found: claude-x", None, "model_not_found"),
        ("", 404, "model_not_found"),
        ("API Error: Overloaded", None, "overloaded"),
        ("", 529, "overloaded"),
        ("", 503, "server_error"),
        ("prompt is too long", 400, "invalid_request"),
        ("Error: Reached max turns (3)", None, "max_turns"),
        ("etwas Seltsames", None, "unknown"),
    ],
)
def test_classify_error_text(text: str, status: int | None, expected: str) -> None:
    assert classify_error_text(text, status) == expected


def test_limit_wait_from_epoch() -> None:
    epoch = int(NOW.timestamp()) + 5400
    assert limit_wait_seconds(f"Claude AI usage limit reached|{epoch}", now=NOW) == 5400
    # Epoche hat Vorrang vor einer Uhrzeit im Text.
    assert limit_wait_seconds(f"limit reached|{epoch} resets 3pm", now=NOW) == 5400


def test_limit_wait_ignores_implausible_epoch() -> None:
    past = int(NOW.timestamp()) - 60
    far = int(NOW.timestamp()) + 9 * 24 * 3600
    assert limit_wait_seconds(f"limit reached|{past}", now=NOW) == 30 * 60
    assert limit_wait_seconds(f"limit reached|{far}", now=NOW) == 30 * 60


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("You've hit your limit · resets 3pm (Europe/Berlin)", 30 * 60),
        ("resets 2pm", 23 * 3600 + 30 * 60),  # schon vorbei → morgen
        ("Limit resets at 9:15am", 18 * 3600 + 45 * 60),
        ("resets 12am", 9 * 3600 + 30 * 60),  # Mitternacht
        ("resets 12pm", 21 * 3600 + 30 * 60),  # Mittag, heute schon vorbei
        ("reset 17:45", 3 * 3600 + 15 * 60),  # 24-Stunden-Format
    ],
)
def test_limit_wait_from_reset_time(text: str, seconds: float) -> None:
    assert limit_wait_seconds(text, now=NOW) == pytest.approx(seconds)


def test_limit_wait_default() -> None:
    assert limit_wait_seconds("Nutzungslimit erreicht", now=NOW) == 30 * 60
    assert limit_wait_seconds("resets 25pm", now=NOW) == 30 * 60


def test_parse_version() -> None:
    assert parse_version("2.1.3 (Claude Code)") == (2, 1, 3)
    assert parse_version("kaputt") is None


# ---------------------------------------------------------------- Umgebung


def test_child_env_strips_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-geheim")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-geheim")
    monkeypatch.setenv("JARVIS_TEST_VAR", "bleibt")
    monkeypatch.delenv("MCP_TOOL_TIMEOUT", raising=False)
    env = child_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["JARVIS_TEST_VAR"] == "bleibt"
    assert env["MCP_TOOL_TIMEOUT"] == str(15 * 60 * 1000)
    assert env["CLAUDE_CODE_DISABLE_TERMINAL_TITLE"] == "1"


def test_child_env_extra_and_existing_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TOOL_TIMEOUT", "1234")
    env = child_env({"JARVIS_MODE": "self_improve", "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "0"})
    assert env["MCP_TOOL_TIMEOUT"] == "1234"  # vorhandener Wert bleibt
    assert env["JARVIS_MODE"] == "self_improve"
    assert env["CLAUDE_CODE_DISABLE_TERMINAL_TITLE"] == "0"


# ---------------------------------------------------------------- build_args


def _cli(tmp_path: Path, version: tuple[int, ...] = (2, 0, 14)) -> ClaudeCodeCLI:
    exe = tmp_path / "claude"
    exe.write_text("", encoding="utf-8")
    cli = ClaudeCodeCLI(str(exe))
    cli._version = version  # kein Aufruf von ``claude --version``
    return cli


def _value(args: list[str], flag: str) -> str:
    assert flag in args, f"{flag} fehlt in {args}"
    return args[args.index(flag) + 1]


async def test_build_args_chat_defaults(tmp_path: Path) -> None:
    cli = _cli(tmp_path)
    run = ClaudeCodeRun(prompt="Hallo", cwd=tmp_path, system_prompt="Du bist JARVIS.")
    with _TempFiles(tmp_path / "tmp") as files:
        args = await cli.build_args(run, files)
        system_file = Path(_value(args, "--system-prompt-file"))
        assert system_file.read_text(encoding="utf-8") == "Du bist JARVIS."
    assert not system_file.exists()  # temporäre Datei wird aufgeräumt
    assert args[0] == cli.executable
    assert args[1] == "-p"
    assert _value(args, "--output-format") == "stream-json"
    assert "--verbose" in args and "--include-partial-messages" in args
    assert _value(args, "--tools") == ""  # tools=() → alle eingebauten Werkzeuge aus
    assert "--strict-mcp-config" in args
    assert "--mcp-config" not in args
    assert "--no-session-persistence" in args
    assert "--resume" not in args
    assert "--max-turns" not in args
    assert "--model" not in args and "--effort" not in args
    assert "--permission-prompts" not in args  # Version zu alt
    assert "--bare" not in args


async def test_build_args_agent_mode(tmp_path: Path) -> None:
    cli = _cli(tmp_path, version=(2, 1, 300))
    mcp = tmp_path / "mcp.json"
    run = ClaudeCodeRun(
        prompt="Mach was",
        cwd=tmp_path,
        model="sonnet",
        effort="high",
        append_system_prompt="Zusatz",
        tools=None,
        allowed_tools=["mcp__jarvis", "Read"],
        disallowed_tools=["Bash", "Edit"],
        permission_mode="dontAsk",
        mcp_config=mcp,
        max_turns=25,
        resume_session=SESSION,
        add_dirs=[tmp_path / "a", tmp_path / "b"],
    )
    with _TempFiles(tmp_path / "tmp") as files:
        args = await cli.build_args(run, files)
        append_file = Path(_value(args, "--append-system-prompt-file"))
        assert append_file.read_text(encoding="utf-8") == "Zusatz"
    assert _value(args, "--model") == "sonnet"
    assert _value(args, "--effort") == "high"
    assert "--system-prompt-file" not in args
    assert "--tools" not in args  # None = Standard-Werkzeuge
    assert _value(args, "--allowedTools") == "mcp__jarvis,Read"
    assert _value(args, "--disallowedTools") == "Bash,Edit"
    assert _value(args, "--permission-mode") == "dontAsk"
    assert _value(args, "--permission-prompts") == "none"
    assert _value(args, "--mcp-config") == str(mcp)
    assert "--strict-mcp-config" in args
    assert _value(args, "--max-turns") == "25"
    assert _value(args, "--resume") == SESSION
    assert "--no-session-persistence" not in args
    assert [args[i + 1] for i, a in enumerate(args) if a == "--add-dir"] == [
        str(tmp_path / "a"),
        str(tmp_path / "b"),
    ]
    assert "--bare" not in args


async def test_build_args_selected_tools_and_kept_session(tmp_path: Path) -> None:
    cli = _cli(tmp_path)
    run = ClaudeCodeRun(prompt="x", cwd=tmp_path, tools=["Read", "Grep"], keep_session=True)
    with _TempFiles(tmp_path / "tmp") as files:
        args = await cli.build_args(run, files)
    assert _value(args, "--tools") == "Read,Grep"
    assert "--no-session-persistence" not in args
    assert "--resume" not in args
    assert "--bare" not in args


async def test_build_args_unknown_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _cli(tmp_path)

    async def no_version() -> None:
        return None

    monkeypatch.setattr(cli, "version", no_version)
    with _TempFiles(tmp_path / "tmp") as files:
        args = await cli.build_args(ClaudeCodeRun(prompt="x", cwd=tmp_path), files)
    assert "--permission-prompts" not in args


async def test_build_args_without_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_code, "find_claude_executable", lambda configured=None: None)
    cli = ClaudeCodeCLI()
    with _TempFiles(tmp_path / "tmp") as files, pytest.raises(ClaudeCodeError) as info:
        await cli.build_args(ClaudeCodeRun(prompt="x", cwd=tmp_path), files)
    assert info.value.kind == "not_installed"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-Pfade")
def test_find_claude_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "leer"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert find_claude_executable() is None
    assert find_claude_executable(str(tmp_path / "fehlt")) is None
    configured = tmp_path / "claude-custom"
    configured.write_text("", encoding="utf-8")
    assert find_claude_executable(str(configured)) == str(configured)
    local = tmp_path / "home" / ".local" / "bin" / "claude"
    local.parent.mkdir(parents=True)
    local.write_text("", encoding="utf-8")
    assert find_claude_executable() == str(local)


# ------------------------------------------------- echter Prozess (Fake-CLI)

FAKE_CLAUDE = """\
import json, os, sys, time
args = sys.argv[1:]
if "--version" in args:
    print("2.0.14 (Claude Code)")
    sys.exit(0)
prompt = sys.stdin.read()
mode = os.environ.get("FAKE_MODE", "result")
if mode == "sleep":
    time.sleep(10)
if mode == "crash":
    print("kaputt: keine Verbindung", file=sys.stderr)
    sys.exit(1)
system = ""
if "--system-prompt-file" in args:
    with open(args[args.index("--system-prompt-file") + 1], encoding="utf-8") as fh:
        system = fh.read()
info = {
    "prompt": prompt,
    "system": system,
    "args": args,
    "has_api_key": "ANTHROPIC_API_KEY" in os.environ,
    "cwd": os.getcwd(),
}
print(json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}))
print("keine json-zeile")
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": json.dumps(info), "session_id": "s1", "usage": {}}))
"""

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="Shebang-Skript nur unter POSIX")


def _fake_executable(tmp_path: Path) -> ClaudeCodeCLI:
    script = tmp_path / "bin" / "claude"
    script.parent.mkdir()
    script.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}", encoding="utf-8")
    script.chmod(0o755)
    return ClaudeCodeCLI(str(script))


@posix_only
async def test_cli_stream_runs_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "systmp"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-darf-nicht-durch")
    cli = _fake_executable(tmp_path)
    assert await cli.version() == (2, 0, 14)
    run = ClaudeCodeRun(prompt="Hallo Claude", cwd=tmp_path / "work", system_prompt="Sys")
    events = [e async for e in cli.stream(run)]
    kinds = [e.kind for e in events]
    # Ohne Text-Deltas wird der Ergebnistext als Text nachgereicht – vor dem Ergebnis.
    assert kinds == ["init", "other", "text", "result"]
    info = json.loads(events[2].text)
    assert info["prompt"] == "Hallo Claude"  # über stdin, nicht als Argument
    assert info["system"] == "Sys"
    assert info["has_api_key"] is False
    assert Path(info["cwd"]) == (tmp_path / "work").resolve()
    assert "--bare" not in info["args"]
    assert events[-1].result is not None and events[-1].result.session_id == "s1"
    # Temporäre Prompt-Dateien sind wieder weg.
    assert list((tmp_path / "systmp" / "jarvis-claude").iterdir()) == []


@posix_only
async def test_cli_stream_crash_without_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "systmp"))
    cli = _fake_executable(tmp_path)
    cli._version = (2, 0, 14)
    run = ClaudeCodeRun(prompt="x", cwd=tmp_path / "work", extra_env={"FAKE_MODE": "crash"})
    with pytest.raises(ClaudeCodeError) as info:
        [e async for e in cli.stream(run)]
    assert info.value.kind == "crashed"
    assert "kaputt: keine Verbindung" in str(info.value)


@posix_only
async def test_cli_stream_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "systmp"))
    cli = _fake_executable(tmp_path)
    cli._version = (2, 0, 14)
    run = ClaudeCodeRun(
        prompt="x", cwd=tmp_path / "work", timeout_s=0.5, extra_env={"FAKE_MODE": "sleep"}
    )
    started = time.monotonic()
    with pytest.raises(ClaudeCodeError) as info:
        [e async for e in cli.stream(run)]
    assert info.value.kind == "timeout"
    assert time.monotonic() - started < 8  # Prozess wurde beendet, nicht abgewartet


# ---------------------------------------------------------------- Transkript


def test_render_transcript_simple() -> None:
    assert render_transcript([]) == ""
    assert render_transcript([ChatMessage(role="user", content="Hallo")]) == "Hallo"


def test_render_transcript_with_history() -> None:
    messages = [
        ChatMessage(role="user", content="Wie spät ist es?"),
        ChatMessage(
            role="assistant",
            content="Ich prüfe das.",
            tool_calls=(ToolCall(id="c1", name="uhrzeit", arguments={"zone": "Berlin"}),),
        ),
        ChatMessage(role="tool", content="14:30", tool_call_id="c1", name="uhrzeit"),
        ChatMessage(role="assistant", content="Es ist 14:30."),
        ChatMessage(role="user", content="Danke!"),
    ]
    assert render_transcript(messages) == "\n".join(
        [
            "<verlauf>",
            "[Benutzer]: Wie spät ist es?",
            "[JARVIS]: Ich prüfe das.\n(ruft Werkzeuge auf: uhrzeit({'zone': 'Berlin'}))",
            "[Werkzeug-Ergebnis uhrzeit]: 14:30",
            "[JARVIS]: Es ist 14:30.",
            "</verlauf>",
            "",
            "Neue Nachricht des Benutzers:\nDanke!",
        ]
    )


def test_render_transcript_ending_with_tool_result() -> None:
    messages = [
        ChatMessage(role="user", content="Wetter?"),
        ChatMessage(
            role="assistant",
            tool_calls=(ToolCall(id="c1", name="wetter", arguments={}),),
        ),
        ChatMessage(role="tool", content="Sonnig", tool_call_id="c1"),
    ]
    text = render_transcript(messages)
    assert "[JARVIS]: (ruft Werkzeuge auf: wetter({}))" in text
    assert text.endswith(
        "</verlauf>\n\n[Werkzeug-Ergebnis ]: Sonnig\n\nSetze die Antwort an den Benutzer fort."
    )


# ---------------------------------------------------------------- Anbieter


class FakeCLI:
    """Ersetzt :class:`ClaudeCodeCLI`: liefert vorgegebene Ereignisse statt einen Prozess."""

    def __init__(
        self,
        events: list[CCEvent] | None = None,
        *,
        error: Exception | None = None,
        executable: str | None = "/opt/claude",
        status: Mapping[str, Any] | Exception | None = None,
    ) -> None:
        self.events = events or []
        self.error = error
        self.executable = executable
        self.status = (
            status if status is not None else {"loggedIn": True, "authMethod": "claude.ai"}
        )
        self.runs: list[ClaudeCodeRun] = []

    async def stream(self, run: ClaudeCodeRun) -> AsyncGenerator[CCEvent, None]:
        self.runs.append(run)
        for event in self.events:
            yield event
        if self.error is not None:
            raise self.error

    async def auth_status(self) -> Mapping[str, Any]:
        if isinstance(self.status, Exception):
            raise self.status
        return self.status


def _events(*lines: str) -> list[CCEvent]:
    return [event for line in lines for event in parse_line(line)]


def _provider(tmp_path: Path, cli: FakeCLI, **options: Any) -> ClaudeCodeProvider:
    provider = ClaudeCodeProvider(
        ProviderConfig(options={"workdir": str(tmp_path / "cc"), **options}),
        SecretStore(use_keyring=False),
    )
    provider.cli = cli  # type: ignore[assignment]
    return provider


def _request(**overrides: Any) -> ChatRequest:
    data: dict[str, Any] = {
        "model": "claude-sonnet",
        "provider_model": "sonnet",
        "messages": [ChatMessage(role="user", content="Hallo")],
        "max_tokens": 1000,
        "system": "Du bist JARVIS.",
        "effort": "low",
    }
    data.update(overrides)
    return ChatRequest(**data)


TOOL = ToolSpec(name="uhrzeit", description="Aktuelle Uhrzeit", parameters={"type": "object"})


async def test_provider_streams_text_and_final_response(tmp_path: Path) -> None:
    cli = FakeCLI(_events(INIT, _delta("Hallo"), _delta(" Welt"), _result()))
    provider = _provider(tmp_path, cli)
    chunks = [c async for c in provider.stream(_request())]
    assert [c.text for c in chunks[:-1]] == ["Hallo", " Welt"]
    response = chunks[-1].response
    assert response is not None
    assert response.text == "Hallo Welt"
    assert response.subscription is True
    assert response.cost_usd == 0.0  # total_cost_usd ist nur eine Schätzung, das Abo zahlt
    assert response.provider == "claude_code"
    assert response.model == "claude-sonnet-4-5-20250929"
    assert response.requested_model == "claude-sonnet"
    assert response.stop_reason == "end_turn"
    [segment] = response.usage
    assert segment.model == "claude-sonnet"  # Katalog-ID, damit der Router richtig verbucht
    assert (segment.input_tokens, segment.output_tokens) == (12, 34)
    assert (segment.cache_read_tokens, segment.cache_write_tokens) == (2000, 100)


async def test_provider_chat_mode_run_parameters(tmp_path: Path) -> None:
    cli = FakeCLI(_events(_result()))
    provider = _provider(tmp_path, cli)
    messages = [
        ChatMessage(role="user", content="Hi"),
        ChatMessage(role="assistant", content="Hallo!"),
        ChatMessage(role="user", content="Wie geht's?"),
    ]
    response = await provider.complete(_request(messages=messages))
    assert response.text == "Hallo Welt"  # kein Text-Delta → Ergebnistext
    [run] = cli.runs
    assert run.prompt == render_transcript(messages)
    assert run.cwd == tmp_path / "cc"
    assert run.model == "sonnet"
    assert run.system_prompt == "Du bist JARVIS."
    assert run.tools == ()
    assert run.effort == "low"
    assert run.max_turns == 1
    assert run.timeout_s == 300.0
    assert run.mcp_config is None and run.permission_mode is None
    assert list(run.allowed_tools) == []


async def test_provider_cli_model_option_and_missing_usage(tmp_path: Path) -> None:
    cli = FakeCLI(_events(_result(usage={}, modelUsage={})))
    provider = _provider(tmp_path, cli)
    response = await provider.complete(_request(options={"cli_model": "claude-opus-4-1"}))
    assert cli.runs[0].model == "claude-opus-4-1"
    assert response.model == "claude-sonnet"  # ohne modelUsage: angefragtes Modell
    assert response.input_tokens == 0 and response.output_tokens == 0


async def test_provider_tools_need_bridge(tmp_path: Path) -> None:
    provider = _provider(tmp_path, FakeCLI(_events(_result())))
    with pytest.raises(ProviderRequestError, match="JARVIS-Dienst"):
        await provider.complete(_request(tools=[TOOL]))


async def test_provider_agent_mode_with_bridge(tmp_path: Path) -> None:
    cli = FakeCLI(_events(_delta("Es ist 14:30."), TOOL_USE, TOOL_RESULT, _result()))
    provider = _provider(tmp_path, cli)
    provider.configure(
        workdir=tmp_path / "agent", bridge=ToolBridge(api_base="http://127.0.0.1:8765", token="t")
    )
    response = await provider.complete(
        _request(tools=[TOOL], session_id="sitzung-1", options={"max_turns": 7})
    )
    assert response.text == "Es ist 14:30."
    [run] = cli.runs
    assert run.cwd == tmp_path / "agent"
    assert run.tools == ()  # eingebaute Werkzeuge bleiben aus
    assert list(run.allowed_tools) == ["mcp__jarvis"]
    assert run.permission_mode == "dontAsk"
    assert run.max_turns == 7
    assert run.system_prompt is not None and run.system_prompt.startswith("Du bist JARVIS.")
    assert "JARVIS-Werkzeuge (MCP)" in run.system_prompt
    assert run.mcp_config is not None
    config = json.loads(run.mcp_config.read_text(encoding="utf-8"))
    env = config["mcpServers"]["jarvis"]["env"]
    assert env["JARVIS_SESSION"] == "sitzung-1"
    assert env["JARVIS_TOOLS"] == "uhrzeit"


async def test_provider_agent_mode_default_max_turns(tmp_path: Path) -> None:
    cli = FakeCLI(_events(_result()))
    provider = _provider(tmp_path, cli)
    provider.configure(workdir=tmp_path, bridge=ToolBridge(api_base="http://x", token="t"))
    await provider.complete(_request(tools=[TOOL], system=None))
    assert cli.runs[0].max_turns == 25
    assert cli.runs[0].system_prompt == (
        "\n\nDu kannst die bereitgestellten JARVIS-Werkzeuge (MCP) nutzen."
    )


async def test_provider_assistant_error_is_not_text(tmp_path: Path) -> None:
    error_line = _line(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "API Error: 500"}]},
            "is_api_error_message": True,
        }
    )
    cli = FakeCLI(_events(error_line, _delta("ok"), _result(result="ok")))
    chunks = [c async for c in _provider(tmp_path, cli).stream(_request())]
    assert [c.text for c in chunks if c.text] == ["ok"]


async def test_provider_rate_limit_maps_to_cooldown(tmp_path: Path) -> None:
    reset = int(time.time()) + 3600
    result = _result(
        is_error=True, api_error_status=429, result=f"Claude AI usage limit reached|{reset}"
    )
    provider = _provider(tmp_path, FakeCLI(_events(result)))
    with pytest.raises(ProviderUnavailableError, match="Abo-Limit") as info:
        await provider.complete(_request())
    assert info.value.retry_after_s == pytest.approx(3600, abs=5)
    assert info.value.provider == "claude_code"
    assert info.value.model == "claude-sonnet"


async def test_provider_rate_limit_with_reset_time(tmp_path: Path) -> None:
    result = _result(is_error=True, result="You've hit your limit · resets 3pm (Europe/Berlin)")
    provider = _provider(tmp_path, FakeCLI(_events(result)))
    with pytest.raises(ProviderUnavailableError) as info:
        await provider.complete(_request())
    assert 0 < info.value.retry_after_s <= 24 * 3600


async def test_provider_auth_error_sets_status(tmp_path: Path) -> None:
    result = _result(
        is_error=True, api_error_status=401, result="Invalid API key · Please run /login"
    )
    provider = _provider(tmp_path, FakeCLI(_events(result)))
    assert provider.unavailable_reason() is None
    with pytest.raises(ProviderAuthError, match="nicht angemeldet"):
        await provider.complete(_request())
    assert "nicht angemeldet" in (provider.unavailable_reason() or "")
    provider.refresh()
    assert provider.unavailable_reason() is None


@pytest.mark.parametrize(
    ("result_line", "expected", "match"),
    [
        (
            _result(is_error=True, api_error_status=404, result="model: claude-x"),
            ModelNotFoundError,
            "nicht verfügbar",
        ),
        (
            _result(is_error=True, api_error_status=400, result="prompt is too long"),
            ProviderRequestError,
            "Ungültige Anfrage",
        ),
        (
            _line({"type": "result", "subtype": "error_max_turns", "num_turns": 2}),
            ProviderRequestError,
            "maximale Schrittzahl",
        ),
        (
            _result(is_error=True, result="etwas Seltsames"),
            ProviderUnavailableError,
            "etwas Seltsames",
        ),
    ],
)
async def test_provider_result_error_mapping(
    tmp_path: Path, result_line: str, expected: type[ProviderError], match: str
) -> None:
    provider = _provider(tmp_path, FakeCLI(_events(_delta("Teil"), result_line)))
    with pytest.raises(expected, match=match) as info:
        await provider.complete(_request())
    if isinstance(info.value, ProviderUnavailableError):
        assert info.value.retry_after_s == 0


@pytest.mark.parametrize(
    ("kind", "expected", "match"),
    [
        ("not_installed", ProviderAuthError, "nicht installiert"),
        ("timeout", ProviderUnavailableError, "Zeitüberschreitung"),
        ("crashed", ProviderUnavailableError, "ohne Ergebnis"),
    ],
)
async def test_provider_cli_error_mapping(
    tmp_path: Path, kind: str, expected: type[ProviderError], match: str
) -> None:
    messages = {
        "not_installed": "Claude Code ist nicht installiert",
        "timeout": "Zeitüberschreitung bei Claude Code",
        "crashed": "Claude Code wurde ohne Ergebnis beendet",
    }
    cli = FakeCLI(error=ClaudeCodeError(messages[kind], kind=kind))
    with pytest.raises(expected, match=match) as info:
        await _provider(tmp_path, cli).complete(_request())
    assert isinstance(info.value.__cause__, ClaudeCodeError)


async def test_provider_probe(tmp_path: Path) -> None:
    cli = FakeCLI(status={"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "pro"})
    provider = _provider(tmp_path, cli)
    assert await provider.probe() is None
    assert provider.unavailable_reason() is None

    cli.status = {"loggedIn": False}
    assert "nicht angemeldet" in (await provider.probe() or "")
    assert "nicht angemeldet" in (provider.unavailable_reason() or "")

    cli.status = {"loggedIn": True, "authMethod": "api_key"}
    assert "API-Key" in (await provider.probe() or "")

    cli.status = ClaudeCodeError("antwortet nicht", kind="timeout")
    assert (await provider.probe() or "").startswith("Claude Code nicht nutzbar")


async def test_provider_probe_allows_api_billing_when_configured(tmp_path: Path) -> None:
    cli = FakeCLI(status={"loggedIn": True, "authMethod": "api_key_helper"})
    provider = _provider(tmp_path, cli, allow_api_billing=True)
    assert await provider.probe() is None


async def test_provider_not_installed(tmp_path: Path) -> None:
    provider = _provider(tmp_path, FakeCLI(executable=None))
    assert "nicht installiert" in (await provider.probe() or "")
    assert "nicht installiert" in (provider.unavailable_reason() or "")
    disabled = ClaudeCodeProvider(ProviderConfig(enabled=False), SecretStore(use_keyring=False))
    assert disabled.unavailable_reason() == "Anbieter deaktiviert"


def test_provider_tools_unavailable_reason(tmp_path: Path) -> None:
    provider = _provider(tmp_path, FakeCLI())
    plain = ModelConfig(id="claude-haiku", provider="claude_code")
    agent = ModelConfig(id="claude-sonnet", provider="claude_code", supports_tools=True)
    assert provider.tools_unavailable_reason(plain) == "keine Werkzeug-Unterstützung"
    assert "Hintergrunddienst" in (provider.tools_unavailable_reason(agent) or "")
    provider.configure(workdir=tmp_path, bridge=ToolBridge(api_base="http://x", token="t"))
    assert provider.tools_unavailable_reason(agent) is None


async def test_router_books_subscription_at_zero_cost(
    tmp_path: Path, db: Database, costs: CostTracker
) -> None:
    # Selbst mit Preisen im Katalog kostet eine Abo-Antwort nichts.
    config = RouterConfig.model_validate(
        {
            "providers": {"claude_code": {}},
            "models": [
                {
                    "id": "claude-sonnet",
                    "provider": "claude_code",
                    "provider_model": "sonnet",
                    "input_cost_per_mtok": 3.0,
                    "output_cost_per_mtok": 15.0,
                }
            ],
            "routes": {"chat": ["claude-sonnet"]},
        }
    )
    provider = _provider(tmp_path, FakeCLI(_events(_delta("Hallo"), _result())))
    router = ModelRouter(config, {"claude_code": provider}, costs)
    response = await router.complete([ChatMessage(role="user", content="Hallo")])
    assert response.text == "Hallo"
    assert response.cost_usd == 0.0
    rows = [dict(r) for r in db.query("SELECT * FROM model_usage")]
    assert len(rows) == 1
    assert rows[0]["cost_usd"] == 0.0
    assert rows[0]["input_tokens"] == 12 and rows[0]["output_tokens"] == 34
