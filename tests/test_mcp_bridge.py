"""MCP-Brücke: Konfiguration für Claude Code und JSON-RPC über stdin/stdout."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from jarvis.core.config import PROJECT_ROOT
from jarvis.integrations import mcp_bridge
from jarvis.integrations.mcp_bridge import SERVER_NAME, ToolBridge, serve_stdio

API = "http://127.0.0.1:8765"
TOKEN = "geheim-123"

TOOL_SPECS = [
    {
        "name": "weather",
        "description": "Wetter abfragen",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        "category": "info",
    },
    {
        "name": "image_generate",
        "description": "Bild erzeugen",
        "parameters": {"type": "object", "properties": {}},
        "category": "bilder",
    },
]

Handler = Callable[[httpx.Request], httpx.Response]


class FakeApi:
    """Ersetzt den JARVIS-Dienst und merkt sich alle Anfragen."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.call_reply: dict[str, Any] | Handler = {"ok": True, "text": "Sonnig, 21 °C"}
        self.list_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/api/tools" and request.method == "GET":
            return httpx.Response(self.list_status, json={"tools": TOOL_SPECS})
        if request.url.path == "/api/tools/call" and request.method == "POST":
            if callable(self.call_reply):
                return self.call_reply(request)
            return httpx.Response(200, json=self.call_reply)
        return httpx.Response(404, json={"detail": "unbekannt"})


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    """Leitet den httpx.Client der Brücke auf einen MockTransport um."""
    api = FakeApi()
    real_client = httpx.Client

    def make_client(**kwargs: Any) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(api), **kwargs)

    monkeypatch.setattr(mcp_bridge.httpx, "Client", make_client)
    return api


def _run(
    messages: list[dict[str, Any] | str],
    *,
    session_id: str = "sess-1",
    tools: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Schickt Nachrichten zeilenweise durch serve_stdio und liefert die Antworten."""
    lines = [m if isinstance(m, str) else json.dumps(m) for m in messages]
    stdin = io.StringIO("\n".join(lines) + "\n")
    stdout = io.StringIO()
    serve_stdio(
        API,
        TOKEN,
        session_id,
        ["weather", "image_generate"] if tools is None else tools,
        stdin=stdin,
        stdout=stdout,
    )
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def _request(msg_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


# ---------------------------------------------------------------------- Konfiguration


def test_write_config_describes_stdio_server(tmp_path: Path) -> None:
    bridge = ToolBridge(api_base=API, token=TOKEN, python="/usr/bin/python3")
    path = bridge.write_config(
        tmp_path / "mcp", session_id="abc", tool_names=["weather", "image_generate"]
    )

    assert path == tmp_path / "mcp" / "mcp-abc.json"
    server = json.loads(path.read_text(encoding="utf-8"))["mcpServers"][SERVER_NAME]
    assert server["type"] == "stdio"
    assert server["command"] == "/usr/bin/python3"
    assert server["args"] == ["-m", "jarvis", "mcp-bridge"]
    env = server["env"]
    # Ohne PYTHONPATH fände Claude Code das Paket in seinem Arbeitsordner nicht.
    assert env["PYTHONPATH"] == str(PROJECT_ROOT)
    assert env["JARVIS_API"] == API
    assert env["JARVIS_API_TOKEN"] == TOKEN
    assert env["JARVIS_SESSION"] == "abc"
    assert env["JARVIS_TOOLS"] == "weather,image_generate"


def test_write_config_without_session_uses_default_name(tmp_path: Path) -> None:
    path = ToolBridge(API, TOKEN).write_config(tmp_path, session_id=None, tool_names=[])
    assert path.name == "mcp-default.json"
    env = json.loads(path.read_text(encoding="utf-8"))["mcpServers"][SERVER_NAME]["env"]
    assert env["JARVIS_SESSION"] == ""
    assert env["JARVIS_TOOLS"] == ""


def test_allowed_tool_rule_covers_all_bridge_tools() -> None:
    assert ToolBridge.allowed_tool_rule() == f"mcp__{SERVER_NAME}"


# ---------------------------------------------------------------------- Protokoll


def test_initialize_echoes_protocol_version(fake_api: FakeApi) -> None:
    replies = _run(
        [
            _request(1, "initialize", {"protocolVersion": "2025-03-26"}),
            _request(2, "initialize"),
        ]
    )
    assert replies[0]["id"] == 1
    result = replies[0]["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["serverInfo"]["name"] == SERVER_NAME
    assert "tools" in result["capabilities"]
    # Ohne Angabe: eigene Standardversion
    assert replies[1]["result"]["protocolVersion"] == mcp_bridge._DEFAULT_PROTOCOL
    assert fake_api.requests == []  # initialize braucht den Dienst nicht


def test_ping_and_notifications(fake_api: FakeApi) -> None:
    replies = _run(
        [
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "method": "tools/list"},  # ohne id: keine Antwort, kein Abruf
            _request(7, "ping"),
        ]
    )
    assert replies == [{"jsonrpc": "2.0", "id": 7, "result": {}}]
    assert fake_api.requests == []


def test_unknown_method_returns_rpc_error(fake_api: FakeApi) -> None:
    (reply,) = _run([_request(3, "resources/list")])
    assert reply["id"] == 3
    assert reply["error"]["code"] == -32601
    assert "resources/list" in reply["error"]["message"]
    assert "result" not in reply


def test_parse_error_and_blank_lines(fake_api: FakeApi) -> None:
    replies = _run(["", "{kein json", "   ", "[1, 2]", _request(4, "ping")])
    assert replies[0] == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": "Parse error"},
    }
    # Leerzeilen und Nicht-Objekte werden ignoriert, danach geht es normal weiter.
    assert replies[1:] == [{"jsonrpc": "2.0", "id": 4, "result": {}}]


# ---------------------------------------------------------------------- tools/list


def test_tools_list_fetches_specs_with_token(fake_api: FakeApi) -> None:
    (reply,) = _run([_request(1, "tools/list")])
    tools = reply["result"]["tools"]
    assert [t["name"] for t in tools] == ["weather", "image_generate"]
    assert tools[0] == {
        "name": "weather",
        "description": "Wetter abfragen",
        "inputSchema": TOOL_SPECS[0]["parameters"],
    }
    (request,) = fake_api.requests
    assert request.url.path == "/api/tools"
    assert request.url.params["names"] == "weather,image_generate"
    assert request.headers["X-Jarvis-Token"] == TOKEN


def test_tools_list_without_filter_sends_empty_names(fake_api: FakeApi) -> None:
    _run([_request(1, "tools/list")], tools=[])
    assert fake_api.requests[0].url.params["names"] == ""


def test_tools_list_http_error_becomes_internal_error(fake_api: FakeApi) -> None:
    fake_api.list_status = 401
    (reply,) = _run([_request(1, "tools/list")])
    assert reply["error"]["code"] == -32603
    assert "401" in reply["error"]["message"]


# ---------------------------------------------------------------------- tools/call


def test_tools_call_forwards_to_jarvis(fake_api: FakeApi) -> None:
    (reply,) = _run([_request(5, "tools/call", {"name": "weather", "arguments": {"city": "Wien"}})])
    assert reply["result"] == {
        "content": [{"type": "text", "text": "Sonnig, 21 °C"}],
        "isError": False,
    }
    (request,) = fake_api.requests
    assert request.method == "POST"
    assert request.url.path == "/api/tools/call"
    assert request.headers["X-Jarvis-Token"] == TOKEN
    assert json.loads(request.content) == {
        "name": "weather",
        "arguments": {"city": "Wien"},
        "session_id": "sess-1",
        "source": "claude_code",
    }


def test_tools_call_without_session_and_arguments(fake_api: FakeApi) -> None:
    _run([_request(5, "tools/call", {"name": "weather"})], session_id="")
    body = json.loads(fake_api.requests[0].content)
    assert body["session_id"] is None
    assert body["arguments"] == {}


def test_tools_call_failed_tool_sets_is_error(fake_api: FakeApi) -> None:
    fake_api.call_reply = {"ok": False, "text": "Abgelehnt: Bestätigung verweigert"}
    (reply,) = _run([_request(6, "tools/call", {"name": "weather", "arguments": {}})])
    assert reply["result"]["isError"] is True
    assert reply["result"]["content"][0]["text"] == "Abgelehnt: Bestätigung verweigert"


def test_tools_call_with_images_returns_tool_text(fake_api: FakeApi) -> None:
    # Bilder erscheinen über den Ereignisbus im Chat; Claude bekommt den Werkzeugtext,
    # der die Bilder bereits erwähnt.
    fake_api.call_reply = {
        "ok": True,
        "text": "Bild erstellt und angezeigt (1024×1024): Katze",
        "images": [{"url": "/api/images/file/a.png", "caption": "Katze", "path": "a.png"}],
    }
    (reply,) = _run([_request(8, "tools/call", {"name": "image_generate", "arguments": {}})])
    assert reply["result"] == {
        "content": [{"type": "text", "text": "Bild erstellt und angezeigt (1024×1024): Katze"}],
        "isError": False,
    }


def test_tools_call_rejects_tool_outside_session(fake_api: FakeApi) -> None:
    (reply,) = _run([_request(9, "tools/call", {"name": "shell_run", "arguments": {}})])
    assert reply["result"]["isError"] is True
    assert "shell_run" in reply["result"]["content"][0]["text"]
    assert fake_api.requests == []  # gar nicht erst an JARVIS weitergereicht


def test_tools_call_without_filter_allows_any_tool(fake_api: FakeApi) -> None:
    (reply,) = _run([_request(9, "tools/call", {"name": "shell_run", "arguments": {}})], tools=[])
    assert reply["result"]["isError"] is False
    assert json.loads(fake_api.requests[0].content)["name"] == "shell_run"


def _server_error(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json={"detail": "kaputt"})


def _connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


@pytest.mark.parametrize("failure", [_server_error, _connect_error], ids=["http-500", "verbindung"])
def test_tools_call_service_errors_become_tool_errors(fake_api: FakeApi, failure: Handler) -> None:
    fake_api.call_reply = failure
    (reply,) = _run([_request(10, "tools/call", {"name": "weather", "arguments": {}})])
    # Kein JSON-RPC-Fehler: Claude soll die Meldung lesen und reagieren können.
    assert "error" not in reply
    assert reply["result"]["isError"] is True
    assert "JARVIS-Dienst nicht erreichbar" in reply["result"]["content"][0]["text"]


def test_session_of_multiple_messages_keeps_order(fake_api: FakeApi) -> None:
    replies = _run(
        [
            _request(1, "initialize", {"protocolVersion": "2025-06-18"}),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            _request(2, "tools/list"),
            _request(3, "tools/call", {"name": "weather", "arguments": {"city": "Graz"}}),
        ]
    )
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert all(r["jsonrpc"] == "2.0" for r in replies)


def test_handle_returns_none_for_notification(fake_api: FakeApi) -> None:
    bridge = mcp_bridge._Bridge(API, TOKEN, "", [])
    assert bridge.handle({"jsonrpc": "2.0", "method": "notifications/cancelled"}) is None
    reply = bridge.handle({"jsonrpc": "2.0", "id": 0, "method": "ping"})
    assert reply == {"jsonrpc": "2.0", "id": 0, "result": {}}  # id 0 ist eine gültige Anfrage
