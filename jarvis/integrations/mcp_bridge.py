"""MCP-Brücke: gibt Claude Code Zugriff auf die Werkzeuge von JARVIS.

Claude Code spricht das Model Context Protocol (MCP). Diese Datei enthält

* :class:`ToolBridge` – erzeugt die MCP-Konfiguration für einen Claude-Code-Lauf,
* :func:`serve_stdio` – einen minimalen MCP-Server (JSON-RPC über stdin/stdout),
  den Claude Code als Kindprozess startet.

Der Server führt Werkzeuge nicht selbst aus, sondern leitet jeden Aufruf an
den laufenden JARVIS-Dienst weiter (``POST /api/tools/call``). Dadurch gelten
Berechtigungsprüfung, Bestätigungen, Not-Aus und Aktionsprotokoll genauso wie
bei allen anderen Modellen. Bewusst ohne MCP-SDK implementiert: Der benötigte
Teil des Protokolls ist klein, und das SDK ändert seine API häufig.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

import httpx

from jarvis.core.config import PROJECT_ROOT

SERVER_NAME = "jarvis"
_DEFAULT_PROTOCOL = "2025-06-18"
_CALL_TIMEOUT_S = 15 * 60


@dataclass(frozen=True)
class ToolBridge:
    """Verbindungsdaten zum laufenden JARVIS-Dienst."""

    api_base: str
    token: str
    python: str = sys.executable

    def write_config(
        self, directory: Path, *, session_id: str | None, tool_names: Sequence[str]
    ) -> Path:
        """Schreibt eine MCP-Konfiguration für ``claude --mcp-config``."""
        directory.mkdir(parents=True, exist_ok=True)
        config = {
            "mcpServers": {
                SERVER_NAME: {
                    "type": "stdio",
                    "command": self.python,
                    "args": ["-m", "jarvis", "mcp-bridge"],
                    "env": {
                        # Claude Code startet die Brücke in seinem eigenen Arbeitsordner –
                        # ohne PYTHONPATH fände ``python -m jarvis`` das Paket nicht.
                        "PYTHONPATH": str(PROJECT_ROOT),
                        "PYTHONIOENCODING": "utf-8",
                        "JARVIS_API": self.api_base,
                        "JARVIS_API_TOKEN": self.token,
                        "JARVIS_SESSION": session_id or "",
                        "JARVIS_TOOLS": ",".join(tool_names),
                    },
                }
            }
        }
        path = directory / f"mcp-{session_id or 'default'}.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    @staticmethod
    def allowed_tool_rule() -> str:
        """Berechtigungsregel, die alle JARVIS-Werkzeuge in Claude Code freigibt."""
        return f"mcp__{SERVER_NAME}"


class _Bridge:
    def __init__(self, api_base: str, token: str, session_id: str, tool_names: list[str]):
        self._client = httpx.Client(
            base_url=api_base,
            headers={"X-Jarvis-Token": token},
            timeout=httpx.Timeout(_CALL_TIMEOUT_S, connect=10),
        )
        self._session = session_id or None
        self._tools = tool_names

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        msg_id = message.get("id")
        if msg_id is None:  # Benachrichtigung (z. B. notifications/initialized)
            return None
        try:
            result = self._dispatch(str(method), message.get("params") or {})
        except _RpcError as exc:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": exc.code, "message": str(exc)},
            }
        except Exception as exc:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32603, "message": str(exc)}}
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return {
                "protocolVersion": params.get("protocolVersion") or _DEFAULT_PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": "1.0"},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            response = self._client.get("/api/tools", params={"names": ",".join(self._tools)})
            response.raise_for_status()
            tools = [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "inputSchema": t["parameters"],
                }
                for t in response.json()["tools"]
            ]
            return {"tools": tools}
        if method == "tools/call":
            name = str(params.get("name"))
            if self._tools and name not in self._tools:
                return _tool_error(f"Werkzeug '{name}' ist in dieser Sitzung nicht freigegeben")
            try:
                response = self._client.post(
                    "/api/tools/call",
                    json={
                        "name": name,
                        "arguments": params.get("arguments") or {},
                        "session_id": self._session,
                        "source": "claude_code",
                    },
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                return _tool_error(f"JARVIS-Dienst nicht erreichbar: {exc}")
            data = response.json()
            return {
                "content": [{"type": "text", "text": data.get("text", "")}],
                "isError": not data.get("ok", False),
            }
        raise _RpcError(-32601, f"Methode nicht unterstützt: {method}")


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _tool_error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def serve_stdio(
    api_base: str,
    token: str,
    session_id: str,
    tool_names: list[str],
    *,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
) -> None:
    """Liest JSON-RPC-Nachrichten zeilenweise von stdin und antwortet auf stdout."""
    reader = stdin or sys.stdin
    writer = stdout or sys.stdout
    bridge = _Bridge(api_base, token, session_id, tool_names)
    for line in reader:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            reply: dict[str, Any] | None = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
        else:
            reply = bridge.handle(message) if isinstance(message, dict) else None
        if reply is not None:
            writer.write(json.dumps(reply, ensure_ascii=False) + "\n")
            writer.flush()
