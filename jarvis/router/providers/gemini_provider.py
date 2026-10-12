"""Anbieter-Plugin: Google Gemini über den kostenlosen API-Key aus Google AI Studio.

Kostenlos nutzbar sind u. a. ``gemini-3.8-flash`` und ``gemini-3.5-flash-lite``
(mit Limits pro Minute/Tag). Wichtig: Im kostenlosen Tarif darf Google Inhalte
zur Verbesserung seiner Produkte verwenden. Deshalb sind diese Modelle in der
Konfiguration als nicht vertrauenswürdig markiert und bekommen keine privaten
Daten (E-Mails, Kalender, Dateien).

Gemini 3 verlangt, dass "thought signatures" bei Werkzeug-Aufrufen unverändert
zurückgeschickt werden. Deshalb werden die Original-Parts der Antwort in
``provider_data`` aufbewahrt und beim nächsten Schritt wiederverwendet.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import AsyncGenerator, Sequence
from typing import Any, cast

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from jarvis.core.config import ProviderConfig
from jarvis.router.base import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    LLMProvider,
    StreamChunk,
    ToolCall,
    ToolSpec,
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

_PARTS_KEY = "gemini_parts"
_DEFAULT_RATE_PAUSE_S = 60.0
_EFFORT_TO_LEVEL = {
    "low": "LOW",
    "medium": "MEDIUM",
    "high": "HIGH",
    "xhigh": "HIGH",
    "max": "HIGH",
}


def to_contents(messages: Sequence[ChatMessage], provider_name: str) -> list[types.Content]:
    """Neutraler Verlauf → Gemini-``Content``-Liste (Werkzeug-Ergebnisse gebündelt)."""
    contents: list[types.Content] = []
    pending_responses: list[types.Part] = []

    def flush() -> None:
        if pending_responses:
            contents.append(types.Content(role="user", parts=list(pending_responses)))
            pending_responses.clear()

    for message in messages:
        if message.role == "tool":
            pending_responses.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        id=message.tool_call_id,
                        name=message.name or "tool",
                        response={"result": message.content},
                    )
                )
            )
            continue
        flush()
        if message.role == "user":
            contents.append(types.Content(role="user", parts=[types.Part(text=message.content)]))
            continue
        original = (message.provider_data or {}).get(_PARTS_KEY)
        if original and (message.provider_data or {}).get("provider") == provider_name:
            contents.append(types.Content(role="model", parts=list(original)))
            continue
        parts: list[types.Part] = []
        if message.content:
            parts.append(types.Part(text=message.content))
        for call in message.tool_calls:
            parts.append(
                types.Part(
                    function_call=types.FunctionCall(
                        id=call.id, name=call.name, args=dict(call.arguments)
                    )
                )
            )
        if parts:
            contents.append(types.Content(role="model", parts=parts))
    flush()
    return contents


def to_tools(tools: Sequence[ToolSpec]) -> list[types.Tool]:
    if not tools:
        return []
    return [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name=tool.name,
                    description=tool.description,
                    parameters_json_schema=dict(tool.parameters),
                )
                for tool in tools
            ]
        )
    ]


def _retry_delay(details: Any) -> float:
    """Liest ``retryDelay`` (z. B. "37s") aus der Fehlerantwort."""
    text = str(details)
    match = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", text)
    return float(match.group(1)) + 1 if match else _DEFAULT_RATE_PAUSE_S


def map_gemini_error(exc: Exception, *, provider: str, model: str) -> ProviderError:
    if isinstance(exc, genai_errors.APIError):
        code = int(exc.code or 0)
        message = (exc.message or str(exc))[:200]
        if code == 429:
            return ProviderUnavailableError(
                f"Gemini-Kontingent erschöpft ({message})",
                provider=provider,
                model=model,
                retry_after_s=_retry_delay(exc.details),
            )
        if code in (401, 403):
            return ProviderAuthError(
                f"Gemini-API-Key ungültig oder gesperrt ({message})", provider=provider, model=model
            )
        if code == 404:
            return ModelNotFoundError(
                f"Gemini-Modell '{model}' nicht gefunden", provider=provider, model=model
            )
        if code >= 500:
            return ProviderUnavailableError(
                f"Gemini gestört ({code})", provider=provider, model=model
            )
        return ProviderRequestError(
            f"Gemini lehnt die Anfrage ab ({code}): {message}", provider=provider, model=model
        )
    return ProviderUnavailableError(
        f"Gemini nicht erreichbar: {exc}", provider=provider, model=model
    )


@register_provider
class GeminiProvider(LLMProvider):
    name = "gemini"
    default_api_key_name = "GEMINI_API_KEY"

    def __init__(
        self, config: ProviderConfig, secrets: SecretStore, *, instance_name: str | None = None
    ) -> None:
        super().__init__(config, secrets, instance_name=instance_name)
        self._client: genai.Client | None = None

    def refresh(self) -> None:
        super().refresh()
        self._client = None

    def client(self) -> genai.Client:
        if self._client is None:
            key = self.api_key()
            if not key:
                raise ProviderAuthError(
                    f"Kein API-Key ({self.api_key_name})", provider=self.instance_name
                )
            self._client = genai.Client(
                api_key=key,
                http_options=types.HttpOptions(timeout=int(self.config.timeout_s * 1000)),
            )
        return self._client

    def build_config(self, request: ChatRequest) -> types.GenerateContentConfig:
        options = request.options
        config = types.GenerateContentConfig(
            system_instruction=request.system or None,
            max_output_tokens=request.max_tokens,
            tools=to_tools(request.tools) or None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        level = options.get("thinking_level") or (
            _EFFORT_TO_LEVEL.get(request.effort) if request.effort else None
        )
        if level:
            config.thinking_config = types.ThinkingConfig(thinking_level=str(level).upper())
        return config

    async def stream(self, request: ChatRequest) -> AsyncGenerator[StreamChunk, None]:
        client = self.client()
        contents = to_contents(request.messages, self.instance_name)
        config = self.build_config(request)
        parts: list[types.Part] = []
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        usage: types.GenerateContentResponseUsageMetadata | None = None
        finish: str | None = None
        try:
            stream = await client.aio.models.generate_content_stream(
                model=request.target_model, contents=cast(Any, contents), config=config
            )
            async for chunk in stream:
                if chunk.usage_metadata is not None:
                    usage = chunk.usage_metadata
                for candidate in chunk.candidates or []:
                    if candidate.finish_reason is not None:
                        finish = str(
                            getattr(candidate.finish_reason, "value", candidate.finish_reason)
                        )
                    for part in (candidate.content.parts if candidate.content else None) or []:
                        parts.append(part)
                        if part.function_call is not None:
                            fc = part.function_call
                            calls.append(
                                ToolCall(
                                    id=fc.id or f"call_{uuid.uuid4().hex[:12]}",
                                    name=str(fc.name),
                                    arguments=dict(fc.args or {}),
                                )
                            )
                        elif part.text and not part.thought:
                            text_parts.append(part.text)
                            yield StreamChunk(text=part.text)
        except ProviderError:
            raise
        except Exception as exc:
            raise map_gemini_error(exc, provider=self.instance_name, model=request.model) from exc

        input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
        output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0) + int(
            getattr(usage, "thoughts_token_count", 0) or 0
        )
        yield StreamChunk(
            response=ChatResponse(
                text="".join(text_parts),
                provider=self.instance_name,
                model=request.model,
                requested_model=request.model,
                stop_reason=finish,
                usage=[UsageSegment(request.model, input_tokens, output_tokens)],
                tool_calls=calls,
                provider_data={"provider": self.instance_name, _PARTS_KEY: parts},
                refused=finish in {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"},
            )
        )

    async def embed(
        self, texts: Sequence[str], model: str = "gemini-embedding-2"
    ) -> list[list[float]]:
        """Erzeugt Embeddings (kostenlos, aber Daten gehen an Google)."""
        try:
            result = await self.client().aio.models.embed_content(
                model=model, contents=cast(Any, list(texts))
            )
        except Exception as exc:
            raise map_gemini_error(exc, provider=self.instance_name, model=model) from exc
        return [list(e.values or []) for e in result.embeddings or []]
