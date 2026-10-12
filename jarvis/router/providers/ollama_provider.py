"""Anbieter-Plugin: lokale Modelle über Ollama – kostenlos, offline, privat.

Installation unter Windows: ``winget install Ollama.Ollama``. Welche Modelle zu
deinem PC passen, ermittelt die Hardware-Erkennung (``jarvis hw``). Fehlt ein
Modell, schlägt der Router ``ollama pull …`` vor und weicht solange aus.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator, Sequence
from typing import Any

import httpx

from jarvis.core.config import ModelConfig, ProviderConfig
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
    ProviderError,
    ProviderRequestError,
    ProviderUnavailableError,
)
from jarvis.router.registry import register_provider
from jarvis.security.secrets import SecretStore

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:11434"
_PROBE_TTL_S = 60.0


def to_ollama_messages(messages: Sequence[ChatMessage], system: str | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if system:
        result.append({"role": "system", "content": system})
    for message in messages:
        if message.role == "tool":
            result.append(
                {"role": "tool", "content": message.content, "tool_name": message.name or "tool"}
            )
            continue
        entry: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_calls:
            entry["tool_calls"] = [
                {"function": {"name": c.name, "arguments": dict(c.arguments)}}
                for c in message.tool_calls
            ]
        result.append(entry)
    return result


def to_ollama_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": dict(t.parameters),
            },
        }
        for t in tools
    ]


def model_matches(installed: set[str], wanted: str) -> bool:
    """``qwen3`` passt zu ``qwen3:latest``; Tags werden sonst exakt verglichen."""
    if wanted in installed:
        return True
    return bool(":" not in wanted and f"{wanted}:latest" in installed)


@register_provider
class OllamaProvider(LLMProvider):
    name = "ollama"
    default_api_key_name = None

    def __init__(
        self, config: ProviderConfig, secrets: SecretStore, *, instance_name: str | None = None
    ) -> None:
        super().__init__(config, secrets, instance_name=instance_name)
        self.base_url = (config.base_url or DEFAULT_BASE_URL).rstrip("/")
        self._client: httpx.AsyncClient | None = None
        self._installed: set[str] | None = None
        self._reachable: bool | None = None
        self._probed_at = 0.0
        self.transport: httpx.AsyncBaseTransport | None = None  # für Tests

    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.config.timeout_s, connect=5),
                transport=self.transport,
            )
        return self._client

    async def probe(self) -> None:
        """Fragt ab, ob Ollama läuft und welche Modelle installiert sind."""
        try:
            response = await self.http().get("/api/tags", timeout=5)
            response.raise_for_status()
            names = {str(m.get("name")) for m in response.json().get("models", [])}
            self._installed = names
            self._reachable = True
        except (httpx.HTTPError, ValueError):
            self._reachable = False
            self._installed = None
        self._probed_at = time.monotonic()

    @property
    def installed_models(self) -> set[str]:
        return set(self._installed or ())

    @property
    def probe_stale(self) -> bool:
        return time.monotonic() - self._probed_at > _PROBE_TTL_S

    def unavailable_reason(self) -> str | None:
        if not self.config.enabled:
            return "Anbieter deaktiviert"
        if self._reachable is False:
            return "Ollama läuft nicht – 'winget install Ollama.Ollama' bzw. Ollama starten"
        return None

    def model_unavailable_reason(self, model: ModelConfig) -> str | None:
        if self._installed is None:
            return None  # noch nicht geprüft – Versuch erlaubt
        if not model_matches(self._installed, model.target):
            return f"nicht installiert – 'ollama pull {model.target}'"
        return None

    async def stream(self, request: ChatRequest) -> AsyncGenerator[StreamChunk, None]:
        options = dict(request.options)
        payload: dict[str, Any] = {
            "model": request.target_model,
            "messages": to_ollama_messages(request.messages, request.system),
            "stream": True,
            "think": bool(options.pop("think", False)),
            "keep_alive": options.pop("keep_alive", "15m"),
            "options": {"num_predict": request.max_tokens, "num_ctx": options.pop("num_ctx", 8192)},
        }
        payload["options"].update(options)
        if request.tools:
            payload["tools"] = to_ollama_tools(request.tools)

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        final: dict[str, Any] = {}
        try:
            async with self.http().stream("POST", "/api/chat", json=payload) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise self._http_error(
                        response.status_code, body, request.model, target=request.target_model
                    )
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if data.get("error"):
                        raise self._http_error(
                            500, str(data["error"]), request.model, target=request.target_model
                        )
                    message = data.get("message") or {}
                    content = message.get("content") or ""
                    if content:
                        text_parts.append(content)
                        yield StreamChunk(text=content)
                    for call in message.get("tool_calls") or []:
                        function = call.get("function") or {}
                        arguments = function.get("arguments") or {}
                        if isinstance(arguments, str):
                            arguments = json.loads(arguments or "{}")
                        calls.append(
                            ToolCall(
                                id=str(call.get("id") or f"call_{uuid.uuid4().hex[:12]}"),
                                name=str(function.get("name")),
                                arguments=arguments,
                            )
                        )
                    if data.get("done"):
                        final = data
        except ProviderError:
            raise
        except httpx.ConnectError as exc:
            self._reachable = False
            raise ProviderUnavailableError(
                "Ollama nicht erreichbar", provider=self.instance_name, model=request.model
            ) from exc
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise ProviderUnavailableError(
                f"Ollama-Fehler: {exc}", provider=self.instance_name, model=request.model
            ) from exc

        yield StreamChunk(
            response=ChatResponse(
                text="".join(text_parts),
                provider=self.instance_name,
                model=request.model,
                requested_model=request.model,
                stop_reason=str(final.get("done_reason") or "stop"),
                usage=[
                    UsageSegment(
                        request.model,
                        int(final.get("prompt_eval_count") or 0),
                        int(final.get("eval_count") or 0),
                    )
                ],
                tool_calls=calls,
            )
        )

    def _http_error(
        self, status: int, body: str, model: str, *, target: str | None = None
    ) -> ProviderError:
        """``model`` = Katalog-ID (für den Router), ``target`` = Modellname bei Ollama."""
        text = body[:200]
        if status == 404 or "not found" in text.lower():
            return ModelNotFoundError(
                f"Ollama-Modell fehlt – 'ollama pull {target or model}' ({text})",
                provider=self.instance_name,
                model=model,
            )
        if status == 400 and "does not support tools" in text.lower():
            return ModelNotFoundError(
                "Dieses lokale Modell kann keine Werkzeuge nutzen",
                provider=self.instance_name,
                model=model,
            )
        if status >= 500:
            return ProviderUnavailableError(
                f"Ollama: {text}", provider=self.instance_name, model=model
            )
        return ProviderRequestError(
            f"Ollama lehnt ab ({status}): {text}", provider=self.instance_name, model=model
        )

    async def embed(self, texts: Sequence[str], model: str) -> list[list[float]]:
        response = await self.http().post("/api/embed", json={"model": model, "input": list(texts)})
        if response.status_code >= 400:
            raise self._http_error(response.status_code, response.text, model)
        return [list(map(float, v)) for v in response.json().get("embeddings", [])]

    async def pull(self, model: str) -> AsyncGenerator[dict[str, Any], None]:
        """Lädt ein Modell herunter und meldet den Fortschritt."""
        async with self.http().stream(
            "POST", "/api/pull", json={"model": model, "stream": True}, timeout=None
        ) as response:
            async for line in response.aiter_lines():
                if line.strip():
                    yield json.loads(line)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
