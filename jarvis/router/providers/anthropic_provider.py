"""Anbieter-Plugin für Claude über die Anthropic API (offizielles ``anthropic``-SDK).

Besonderheiten:

* Streaming über ``client.beta.messages.stream`` – nötig für den serverseitigen
  Refusal-Fallback (``fallbacks="default"``): Lehnt ein Sicherheitsfilter eine
  harmlose Anfrage fälschlich ab, beantwortet Anthropic sie automatisch mit
  einem passenden anderen Modell, statt sie einfach abzubrechen.
* ``effort`` wird als ``output_config.effort`` übergeben. ``temperature`` &
  Co. sendet JARVIS bewusst nicht – aktuelle Claude-Modelle lehnen sie ab.
* Automatisches Prompt-Caching (``cache_control``) senkt die Kosten bei langen
  Chats, weil der gleichbleibende Verlauf nicht jedes Mal voll berechnet wird.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Sequence
from typing import Any

import anthropic

from jarvis.core.config import ProviderConfig
from jarvis.router.base import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    LLMProvider,
    StreamChunk,
    ToolCall,
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

REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_BLOCKS_KEY = "anthropic_content"


def to_anthropic_messages(messages: Sequence[ChatMessage], provider: str) -> list[dict[str, Any]]:
    """Neutraler Verlauf → Messages-API-Format (Werkzeug-Ergebnisse als ``tool_result``).

    Antworten desselben Anbieters werden mit ihren Original-Blöcken (inkl.
    Thinking-Blöcken) zurückgeschickt – das verlangen aktuelle Claude-Modelle
    bei Werkzeug-Schleifen.
    """
    result: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    def flush() -> None:
        if pending:
            result.append({"role": "user", "content": list(pending)})
            pending.clear()

    for message in messages:
        if message.role == "tool":
            pending.append(
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.content,
                }
            )
            continue
        flush()
        if message.role == "user":
            result.append({"role": "user", "content": message.content})
            continue
        data = message.provider_data or {}
        if data.get("provider") == provider and data.get(_BLOCKS_KEY):
            result.append({"role": "assistant", "content": list(data[_BLOCKS_KEY])})
            continue
        blocks: list[dict[str, Any]] = []
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        for call in message.tool_calls:
            blocks.append(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": dict(call.arguments),
                }
            )
        result.append({"role": "assistant", "content": blocks or message.content})
    flush()
    return result


def map_anthropic_error(exc: anthropic.AnthropicError, *, model: str) -> ProviderError:
    """Übersetzt SDK-Fehler in Router-Fehler (spezifischste Klasse zuerst)."""
    name = AnthropicProvider.name
    detail = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return ProviderAuthError(
            f"Zugriff verweigert – API-Key prüfen ({detail})", provider=name, model=model
        )
    if isinstance(exc, anthropic.NotFoundError):
        return ModelNotFoundError(
            f"Modell '{model}' nicht gefunden oder nicht freigeschaltet", provider=name, model=model
        )
    if isinstance(exc, anthropic.RateLimitError):
        return ProviderUnavailableError("Rate-Limit erreicht", provider=name, model=model)
    if isinstance(exc, anthropic.APIConnectionError):  # inklusive APITimeoutError
        return ProviderUnavailableError(
            f"Netzwerkfehler oder Timeout ({type(exc).__name__})", provider=name, model=model
        )
    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        if status == 402:
            return ProviderAuthError(
                "Abrechnungsproblem (402) – Guthaben/Zahlungsdaten in der Anthropic Console prüfen",
                provider=name,
                model=model,
            )
        if status >= 500:
            return ProviderUnavailableError(
                f"Anthropic vorübergehend gestört ({status})", provider=name, model=model
            )
        return ProviderRequestError(
            f"Ungültige Anfrage ({status}): {detail}", provider=name, model=model
        )
    if isinstance(exc, anthropic.CredentialsError):
        return ProviderAuthError(
            f"Keine gültigen Zugangsdaten ({detail})", provider=name, model=model
        )
    return ProviderUnavailableError(
        f"Unerwarteter SDK-Fehler: {detail}", provider=name, model=model
    )


@register_provider
class AnthropicProvider(LLMProvider):
    name = "anthropic"
    default_api_key_name = "ANTHROPIC_API_KEY"

    def __init__(
        self, config: ProviderConfig, secrets: SecretStore, *, instance_name: str | None = None
    ) -> None:
        super().__init__(config, secrets, instance_name=instance_name)
        self._client: anthropic.AsyncAnthropic | None = None
        self._prompt_caching = bool(config.options.get("prompt_caching", True))

    def _get_client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            api_key = self.api_key()
            if not api_key:
                raise ProviderAuthError(
                    f"Kein API-Key gefunden ({self.api_key_name})", provider=self.instance_name
                )
            self._client = anthropic.AsyncAnthropic(
                api_key=api_key,
                base_url=self.config.base_url,
                timeout=self.config.timeout_s,
                max_retries=self.config.max_retries,
            )
        return self._client

    def build_params(self, request: ChatRequest) -> dict[str, Any]:
        """Übersetzt eine :class:`ChatRequest` in die Parameter des SDK-Aufrufs."""
        params: dict[str, Any] = {
            "model": request.target_model,
            "max_tokens": request.max_tokens,
            "messages": to_anthropic_messages(request.messages, self.instance_name),
        }
        if request.tools:
            params["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": dict(t.parameters)}
                for t in request.tools
            ]
        if request.system:
            params["system"] = request.system
        if request.effort:
            params["output_config"] = {"effort": request.effort}
        if self._prompt_caching:
            params["cache_control"] = {"type": "ephemeral"}
        if request.refusal_fallback:
            params["fallbacks"] = "default"
            params["betas"] = [REFUSAL_FALLBACK_BETA]
        return params

    async def stream(self, request: ChatRequest) -> AsyncGenerator[StreamChunk, None]:
        client = self._get_client()
        params = self.build_params(request)
        try:
            async with client.beta.messages.stream(**params) as stream:
                async for text in stream.text_stream:
                    yield StreamChunk(text=text)
                final = await stream.get_final_message()
        except anthropic.AnthropicError as exc:
            raise map_anthropic_error(exc, model=request.model) from exc
        yield StreamChunk(response=self.to_response(final, request))

    def to_response(self, message: Any, request: ChatRequest) -> ChatResponse:
        """Wandelt die finale SDK-Nachricht in eine :class:`ChatResponse`."""
        text = "".join(block.text for block in message.content if block.type == "text")
        calls = [
            ToolCall(id=str(block.id), name=str(block.name), arguments=dict(block.input or {}))
            for block in message.content
            if block.type == "tool_use"
        ]
        raw_blocks = [
            block.model_dump(mode="json", exclude_none=True)
            if hasattr(block, "model_dump")
            else block
            for block in message.content
        ]
        served_model = str(message.model or request.model)

        segments: list[UsageSegment] = []
        fallback_used = False
        # usage.iterations ist bei Fallbacks die verbindliche Abrechnung pro Versuch;
        # das Top-Level-``usage`` deckt dann nur den letzten Versuch ab.
        for iteration in getattr(message.usage, "iterations", None) or []:
            if iteration.type == "fallback_message":
                fallback_used = True
            segments.append(
                UsageSegment(
                    model=str(getattr(iteration, "model", None) or served_model),
                    input_tokens=iteration.input_tokens or 0,
                    output_tokens=iteration.output_tokens or 0,
                    cache_read_tokens=iteration.cache_read_input_tokens or 0,
                    cache_write_tokens=iteration.cache_creation_input_tokens or 0,
                )
            )
        if not segments:
            usage = message.usage
            segments.append(
                UsageSegment(
                    model=served_model,
                    input_tokens=usage.input_tokens or 0,
                    output_tokens=usage.output_tokens or 0,
                    cache_read_tokens=getattr(usage, "cache_read_input_tokens", None) or 0,
                    cache_write_tokens=getattr(usage, "cache_creation_input_tokens", None) or 0,
                )
            )

        refused = message.stop_reason == "refusal"
        category: str | None = None
        stop_details = getattr(message, "stop_details", None)
        if refused and stop_details is not None:
            category = getattr(stop_details, "category", None)

        return ChatResponse(
            text=text,
            provider=self.instance_name,
            model=served_model,
            requested_model=request.model,
            stop_reason=message.stop_reason,
            usage=segments,
            tool_calls=calls,
            provider_data={"provider": self.instance_name, _BLOCKS_KEY: raw_blocks},
            refused=refused,
            refusal_category=category,
            provider_fallback_used=fallback_used,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
