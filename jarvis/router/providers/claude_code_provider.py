"""Anbieter-Plugin: Claude über Claude Code und dein Claude-Pro-Abo (keine API-Kosten).

Zwei Betriebsarten:

* **Chat** (ohne Werkzeuge): alle eingebauten Werkzeuge von Claude Code sind aus,
  der System-Prompt wird durch den von JARVIS ersetzt. Das spart Abo-Kontingent.
* **Agent** (mit Werkzeugen): Claude Code bekommt die JARVIS-Werkzeuge über die
  MCP-Brücke. Eingebaute Werkzeuge (Bash, Dateien bearbeiten …) bleiben aus –
  alles läuft über die Berechtigungsprüfung von JARVIS.

Claude Code ist zustandslos aufgerufen: Der Gesprächsverlauf wird als Text
mitgeschickt.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path

from jarvis.core.config import ModelConfig, ProviderConfig
from jarvis.integrations.claude_code import (
    ClaudeCodeCLI,
    ClaudeCodeError,
    ClaudeCodeRun,
    limit_wait_seconds,
)
from jarvis.integrations.mcp_bridge import ToolBridge
from jarvis.router.base import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    LLMProvider,
    StreamChunk,
    UsageSegment,
)
from jarvis.router.errors import (
    ModelNotFoundError,
    ProviderAuthError,
    ProviderError,
    ProviderRequestError,
    ProviderUnavailableError,
)
from jarvis.router.registry import register_provider
from jarvis.security.secrets import SecretStore

log = logging.getLogger(__name__)

_ROLE_LABELS = {"user": "Benutzer", "assistant": "JARVIS"}
_INSTALL_HINT = "Claude Code nicht installiert – 'winget install Anthropic.ClaudeCode'"
_LOGIN_HINT = "Claude Code nicht angemeldet – im Terminal 'claude' starten und mit Pro anmelden"


def render_transcript(messages: Sequence[ChatMessage]) -> str:
    """Formt den Verlauf in einen Prompt für den zustandslosen CLI-Aufruf."""
    if not messages:
        return ""
    *history, last = messages
    if not history and last.role == "user":
        return last.content
    lines = ["<verlauf>"]
    for message in history:
        lines.append(_render(message))
    lines.append("</verlauf>")
    lines.append("")
    if last.role == "user":
        lines.append(f"Neue Nachricht des Benutzers:\n{last.content}")
    else:
        lines.append(_render(last))
        lines.append("\nSetze die Antwort an den Benutzer fort.")
    return "\n".join(lines)


def _render(message: ChatMessage) -> str:
    if message.role == "tool":
        return f"[Werkzeug-Ergebnis {message.name or ''}]: {message.content}"
    label = _ROLE_LABELS.get(message.role, message.role)
    text = message.content
    if message.tool_calls:
        calls = ", ".join(f"{c.name}({dict(c.arguments)})" for c in message.tool_calls)
        text = f"{text}\n(ruft Werkzeuge auf: {calls})".strip()
    return f"[{label}]: {text}"


@register_provider
class ClaudeCodeProvider(LLMProvider):
    name = "claude_code"
    default_api_key_name = None

    def __init__(
        self, config: ProviderConfig, secrets: SecretStore, *, instance_name: str | None = None
    ) -> None:
        super().__init__(config, secrets, instance_name=instance_name)
        self.cli = ClaudeCodeCLI(config.options.get("executable"))
        self.workdir = Path(config.options.get("workdir") or Path.home() / ".jarvis-claude")
        self.bridge: ToolBridge | None = None
        self._status_reason: str | None = None
        self._allow_api_billing = bool(config.options.get("allow_api_billing", False))

    def configure(self, *, workdir: Path, bridge: ToolBridge | None = None) -> None:
        self.workdir = workdir
        self.bridge = bridge

    async def probe(self) -> str | None:
        """Prüft Installation und Login einmalig (dauert 1–2 Sekunden)."""
        if self.cli.executable is None:
            self._status_reason = _INSTALL_HINT
            return self._status_reason
        try:
            status = await self.cli.auth_status()
        except ClaudeCodeError as exc:
            self._status_reason = f"Claude Code nicht nutzbar: {exc}"
            return self._status_reason
        method = str(status.get("authMethod") or "")
        if not status.get("loggedIn"):
            self._status_reason = _LOGIN_HINT
        elif method in {"api_key", "api_key_helper"} and not self._allow_api_billing:
            self._status_reason = (
                "Claude Code ist mit einem API-Key angemeldet (kostet Geld) – "
                "mit 'claude auth login' auf dein Pro-Abo umstellen"
            )
        else:
            self._status_reason = None
        return self._status_reason

    def refresh(self) -> None:
        super().refresh()
        self._status_reason = None

    def unavailable_reason(self) -> str | None:
        if not self.config.enabled:
            return "Anbieter deaktiviert"
        if self.cli.executable is None:
            return _INSTALL_HINT
        return self._status_reason

    def tools_unavailable_reason(self, model: ModelConfig) -> str | None:
        if not model.supports_tools:
            return "keine Werkzeug-Unterstützung"
        if self.bridge is None:
            return "Werkzeuge nur im JARVIS-Hintergrunddienst"
        return None

    async def stream(self, request: ChatRequest) -> AsyncGenerator[StreamChunk, None]:
        options = request.options
        run = ClaudeCodeRun(
            prompt=render_transcript(request.messages),
            cwd=self.workdir,
            model=str(options.get("cli_model") or request.target_model),
            system_prompt=(request.system or "")
            + (
                "\n\nDu kannst die bereitgestellten JARVIS-Werkzeuge (MCP) nutzen."
                if request.tools
                else ""
            ),
            tools=(),
            effort=request.effort,
            timeout_s=self.config.timeout_s,
            max_turns=int(options.get("max_turns", 25)) if request.tools else 1,
        )
        if request.tools:
            if self.bridge is None:
                raise ProviderRequestError(
                    "Werkzeuge über Claude Code brauchen den JARVIS-Dienst",
                    provider=self.instance_name,
                    model=request.model,
                )
            run.mcp_config = self.bridge.write_config(
                self.workdir / "mcp",
                session_id=request.session_id,
                tool_names=[t.name for t in request.tools],
            )
            run.allowed_tools = [ToolBridge.allowed_tool_rule()]
            run.permission_mode = "dontAsk"

        text_parts: list[str] = []
        try:
            async for event in self.cli.stream(run):
                if event.kind == "text":
                    text_parts.append(event.text)
                    yield StreamChunk(text=event.text)
                elif event.kind == "assistant_error":
                    log.debug("Claude Code meldet: %s", event.text)
                elif event.kind == "result" and event.result is not None:
                    result = event.result
                    if result.is_error:
                        raise self._map_error(result.error_kind, result.text, request.model)
                    usage = result.usage
                    yield StreamChunk(
                        response=ChatResponse(
                            text="".join(text_parts) or result.text,
                            provider=self.instance_name,
                            model=result.model or request.model,
                            requested_model=request.model,
                            stop_reason="end_turn",
                            usage=[
                                UsageSegment(
                                    model=request.model,
                                    input_tokens=int(usage.get("input_tokens") or 0),
                                    output_tokens=int(usage.get("output_tokens") or 0),
                                    cache_read_tokens=int(
                                        usage.get("cache_read_input_tokens") or 0
                                    ),
                                    cache_write_tokens=int(
                                        usage.get("cache_creation_input_tokens") or 0
                                    ),
                                )
                            ],
                            subscription=True,
                        )
                    )
                    return
        except ClaudeCodeError as exc:
            raise self._map_error(exc.kind, str(exc), request.model) from exc

    def _map_error(self, kind: str | None, text: str, model: str) -> ProviderError:
        name = self.instance_name
        if kind in {"authentication_failed", "oauth_org_not_allowed", "account_on_hold"}:
            self._status_reason = _LOGIN_HINT
            return ProviderAuthError(f"{_LOGIN_HINT} ({text[:120]})", provider=name, model=model)
        if kind == "billing_error":
            return ProviderAuthError(f"Abo-Problem: {text[:160]}", provider=name, model=model)
        if kind == "not_installed":
            return ProviderAuthError(_INSTALL_HINT, provider=name, model=model)
        if kind == "rate_limit":
            wait = limit_wait_seconds(text)
            return ProviderUnavailableError(
                f"Claude-Abo-Limit erreicht ({text[:120]})",
                provider=name,
                model=model,
                retry_after_s=wait,
            )
        if kind == "model_not_found":
            return ModelNotFoundError(
                f"Modell nicht verfügbar: {text[:120]}", provider=name, model=model
            )
        if kind in {"invalid_request", "max_output_tokens"}:
            return ProviderRequestError(
                f"Ungültige Anfrage: {text[:160]}", provider=name, model=model
            )
        if kind == "max_turns":
            return ProviderRequestError(
                "Claude Code hat die maximale Schrittzahl erreicht", provider=name, model=model
            )
        return ProviderUnavailableError(f"Claude Code: {text[:200]}", provider=name, model=model)
