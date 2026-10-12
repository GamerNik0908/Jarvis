"""Anbieter-Plugin für OpenAI-kompatible Schnittstellen (``/v1/chat/completions``).

Damit lassen sich weitere Dienste mit kostenlosem Kontingent anbinden, z. B.
Groq, OpenRouter (Modelle mit ``:free``), Mistral oder LM Studio lokal. Ein
Eintrag in ``config.yaml`` genügt::

    router:
      providers:
        groq:
          type: openai_compat
          base_url: https://api.groq.com/openai/v1
          api_key_name: GROQ_API_KEY
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator, Sequence
from typing import Any

import httpx

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


def to_openai_messages(messages: Sequence[ChatMessage], system: str | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if system:
        result.append({"role": "system", "content": system})
    for message in messages:
        if message.role == "tool":
            result.append(
                {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content}
            )
            continue
        entry: dict[str, Any] = {"role": message.role, "content": message.content or None}
        if message.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(dict(c.arguments))},
                }
                for c in message.tool_calls
            ]
        if entry["content"] is None and not message.tool_calls:
            entry["content"] = ""
        result.append(entry)
    return result


def to_openai_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
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


@register_provider
class OpenAICompatProvider(LLMProvider):
    name = "openai_compat"
    default_api_key_name = None

    def __init__(
        self, config: ProviderConfig, secrets: SecretStore, *, instance_name: str | None = None
    ) -> None:
        super().__init__(config, secrets, instance_name=instance_name)
        self._client: httpx.AsyncClient | None = None
        self.transport: httpx.AsyncBaseTransport | None = None  # für Tests

    def unavailable_reason(self) -> str | None:
        if not self.config.base_url:
            return "base_url fehlt in config.yaml"
        return super().unavailable_reason()

    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {}
            key = self.api_key()
            if key:
                headers["Authorization"] = f"Bearer {key}"
            self._client = httpx.AsyncClient(
                base_url=(self.config.base_url or "").rstrip("/"),
                headers=headers,
                timeout=httpx.Timeout(self.config.timeout_s, connect=10),
                transport=self.transport,
            )
        return self._client

    def refresh(self) -> None:
        super().refresh()
        self._client = None

    async def stream(self, request: ChatRequest) -> AsyncGenerator[StreamChunk, None]:
        payload: dict[str, Any] = {
            "model": request.target_model,
            "messages": to_openai_messages(request.messages, request.system),
            "max_tokens": request.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.tools:
            payload["tools"] = to_openai_tools(request.tools)
        payload.update(request.options.get("extra_body", {}) or {})

        text_parts: list[str] = []
        partial_calls: dict[int, dict[str, str]] = {}
        usage: dict[str, Any] = {}
        finish: str | None = None
        try:
            async with self.http().stream("POST", "/chat/completions", json=payload) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise self._http_error(response.status_code, body, request.model)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data_text = line[5:].strip()
                    if data_text == "[DONE]":
                        break
                    data = json.loads(data_text)
                    if data.get("usage"):
                        usage = data["usage"]
                    for choice in data.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if choice.get("finish_reason"):
                            finish = choice["finish_reason"]
                        content = delta.get("content")
                        if content:
                            text_parts.append(content)
                            yield StreamChunk(text=content)
                        for call in delta.get("tool_calls") or []:
                            slot = partial_calls.setdefault(
                                int(call.get("index", 0)), {"id": "", "name": "", "args": ""}
                            )
                            slot["id"] = call.get("id") or slot["id"]
                            function = call.get("function") or {}
                            slot["name"] += function.get("name") or ""
                            slot["args"] += function.get("arguments") or ""
        except ProviderError:
            raise
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise ProviderUnavailableError(
                f"{self.instance_name} nicht erreichbar: {exc}",
                provider=self.instance_name,
                model=request.model,
            ) from exc

        calls = []
        for index in sorted(partial_calls):
            slot = partial_calls[index]
            try:
                arguments = json.loads(slot["args"] or "{}")
            except json.JSONDecodeError:
                arguments = {"_raw": slot["args"]}
            calls.append(
                ToolCall(id=slot["id"] or f"call_{index}", name=slot["name"], arguments=arguments)
            )
        yield StreamChunk(
            response=ChatResponse(
                text="".join(text_parts),
                provider=self.instance_name,
                model=request.model,
                requested_model=request.model,
                stop_reason=finish,
                usage=[
                    UsageSegment(
                        request.model,
                        int(usage.get("prompt_tokens") or 0),
                        int(usage.get("completion_tokens") or 0),
                    )
                ],
                tool_calls=calls,
            )
        )

    def _http_error(self, status: int, body: str, model: str) -> ProviderError:
        text = body[:200]
        name = self.instance_name
        if status in (401, 403):
            return ProviderAuthError(
                f"{name}: Zugriff verweigert ({text})", provider=name, model=model
            )
        if status == 404:
            return ModelNotFoundError(
                f"{name}: Modell nicht gefunden ({text})", provider=name, model=model
            )
        if status == 429:
            return ProviderUnavailableError(
                f"{name}: Kontingent erschöpft", provider=name, model=model, retry_after_s=60
            )
        if status >= 500:
            return ProviderUnavailableError(
                f"{name} gestört ({status})", provider=name, model=model
            )
        return ProviderRequestError(
            f"{name} lehnt ab ({status}): {text}", provider=name, model=model
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
