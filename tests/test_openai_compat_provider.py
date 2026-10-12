"""Tests für das Plugin für OpenAI-kompatible Dienste – SSE-Streaming über ``httpx.MockTransport``."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from jarvis.core.config import ProviderConfig
from jarvis.router.base import ChatMessage, ChatRequest, ToolCall, ToolSpec
from jarvis.router.errors import (
    ModelNotFoundError,
    ProviderAuthError,
    ProviderError,
    ProviderRequestError,
    ProviderUnavailableError,
)
from jarvis.router.providers.openai_compat_provider import (
    OpenAICompatProvider,
    to_openai_messages,
    to_openai_tools,
)
from jarvis.security.secrets import SecretStore

Handler = Callable[[httpx.Request], httpx.Response]

KEY_NAME = "JARVIS_TEST_COMPAT_KEY"
TOOL = ToolSpec(
    name="wetter",
    description="Wetter für eine Stadt",
    parameters={"type": "object", "properties": {"stadt": {"type": "string"}}},
)


def _provider(
    handler: Handler | None = None,
    *,
    base_url: str | None = "https://api.groq.test/openai/v1/",
    api_key_name: str | None = None,
) -> OpenAICompatProvider:
    provider = OpenAICompatProvider(
        ProviderConfig(type="openai_compat", base_url=base_url, api_key_name=api_key_name),
        SecretStore(use_keyring=False),
        instance_name="groq",
    )
    if handler is not None:
        provider.transport = httpx.MockTransport(handler)
    return provider


def _sse(*events: dict[str, Any] | str) -> bytes:
    lines: list[str] = []
    for event in events:
        payload = event if isinstance(event, str) else json.dumps(event)
        lines.append(f"data: {payload}")
        lines.append("")
    return "\n".join(lines).encode("utf-8")


def _delta(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def _request(**overrides: Any) -> ChatRequest:
    data: dict[str, Any] = {
        "model": "groq-llama",
        "provider_model": "llama-3.3-70b-versatile",
        "messages": [ChatMessage(role="user", content="Hallo")],
        "max_tokens": 1024,
    }
    data.update(overrides)
    return ChatRequest(**data)


# ---------------------------------------------------------------- Umwandlungen


def test_to_openai_messages() -> None:
    messages = [
        ChatMessage(role="user", content="Wetter?"),
        ChatMessage(
            role="assistant",
            tool_calls=(ToolCall(id="call_1", name="wetter", arguments={"stadt": "Köln"}),),
        ),
        ChatMessage(role="tool", content="Sonnig", tool_call_id="call_1", name="wetter"),
        ChatMessage(role="assistant", content=""),
        ChatMessage(role="assistant", content="Es ist sonnig."),
    ]
    assert to_openai_messages(messages, "Du bist JARVIS.") == [
        {"role": "system", "content": "Du bist JARVIS."},
        {"role": "user", "content": "Wetter?"},
        {
            "role": "assistant",
            "content": None,  # nur Werkzeug-Aufrufe → content null
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "wetter", "arguments": '{"stadt": "K\\u00f6ln"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "Sonnig"},
        {"role": "assistant", "content": ""},  # nie null ohne Werkzeug-Aufrufe
        {"role": "assistant", "content": "Es ist sonnig."},
    ]
    arguments = to_openai_messages(messages, None)[1]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(arguments) == {"stadt": "Köln"}


def test_to_openai_tools() -> None:
    assert to_openai_tools([]) == []
    assert to_openai_tools([TOOL]) == [
        {
            "type": "function",
            "function": {
                "name": "wetter",
                "description": "Wetter für eine Stadt",
                "parameters": dict(TOOL.parameters),
            },
        }
    ]


# ---------------------------------------------------------------- Verfügbarkeit


def test_unavailable_without_base_url() -> None:
    assert _provider(base_url=None).unavailable_reason() == "base_url fehlt in config.yaml"


def test_unavailable_without_configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(KEY_NAME, raising=False)
    provider = _provider(api_key_name=KEY_NAME)
    assert provider.unavailable_reason() == f"kein API-Key ({KEY_NAME})"
    monkeypatch.setenv(KEY_NAME, "gsk-test-123456")
    provider.refresh()
    assert provider.unavailable_reason() is None


def test_local_server_needs_no_key() -> None:
    provider = _provider(base_url="http://127.0.0.1:1234/v1")
    assert provider.api_key_name is None
    assert provider.unavailable_reason() is None


# ---------------------------------------------------------------- Stream


async def test_stream_text_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY_NAME, "gsk-test-123456")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = _sse(
            _delta({"role": "assistant", "content": ""}),
            _delta({"content": "Hallo"}),
            _delta({"content": " Welt"}),
            _delta({}, finish="stop"),
            {
                "id": "chatcmpl-1",
                "choices": [],
                "usage": {"prompt_tokens": 21, "completion_tokens": 4},
            },
            "[DONE]",
        )
        # Kommentarzeilen (Keep-Alive) werden ignoriert.
        return httpx.Response(200, content=b": keep-alive\n\n" + body)

    provider = _provider(handler, api_key_name=KEY_NAME)
    request = _request(system="Du bist JARVIS.", options={"extra_body": {"temperature": 0.3}})
    chunks = [c async for c in provider.stream(request)]
    assert [c.text for c in chunks[:-1]] == ["Hallo", " Welt"]
    response = chunks[-1].response
    assert response is not None
    assert response.text == "Hallo Welt"
    assert response.stop_reason == "stop"
    assert response.provider == "groq"
    assert response.model == "groq-llama" and response.requested_model == "groq-llama"
    assert response.tool_calls == []
    [segment] = response.usage
    assert (segment.model, segment.input_tokens, segment.output_tokens) == ("groq-llama", 21, 4)

    [sent] = seen
    assert str(sent.url) == "https://api.groq.test/openai/v1/chat/completions"
    assert sent.headers["Authorization"] == "Bearer gsk-test-123456"
    payload = json.loads(sent.content)
    assert payload["model"] == "llama-3.3-70b-versatile"
    assert payload["max_tokens"] == 1024
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["temperature"] == 0.3  # extra_body wird übernommen
    assert payload["messages"][0] == {"role": "system", "content": "Du bist JARVIS."}
    assert "tools" not in payload


async def test_stream_without_key_sends_no_auth_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_sse(_delta({"content": "ok"}), "[DONE]"))

    provider = _provider(handler, base_url="http://127.0.0.1:1234/v1")
    response = await provider.complete(_request())
    assert response.text == "ok"
    assert "Authorization" not in seen[0].headers
    assert response.usage[0].input_tokens == 0  # Server ohne Usage-Angabe


async def test_stream_assembles_tool_calls_from_fragments() -> None:
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        body = _sse(
            _delta({"role": "assistant", "content": None}),
            _delta(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_abc",
                            "type": "function",
                            "function": {"name": "wetter", "arguments": ""},
                        }
                    ]
                }
            ),
            _delta({"tool_calls": [{"index": 0, "function": {"arguments": '{"stadt":'}}]}),
            _delta({"tool_calls": [{"index": 0, "function": {"arguments": ' "Berlin"}'}}]}),
            _delta(
                {
                    "tool_calls": [
                        {
                            "index": 1,
                            "id": "call_def",
                            "function": {"name": "wetter", "arguments": '{"stadt": "Rom"}'},
                        }
                    ]
                }
            ),
            _delta({}, finish="tool_calls"),
            "[DONE]",
        )
        return httpx.Response(200, content=body)

    provider = _provider(handler)
    chunks = [c async for c in provider.stream(_request(tools=[TOOL]))]
    assert len(chunks) == 1  # kein Text, nur die Antwort
    response = chunks[0].response
    assert response is not None
    assert response.stop_reason == "tool_calls"
    assert response.text == ""
    assert [(c.id, c.name, dict(c.arguments)) for c in response.tool_calls] == [
        ("call_abc", "wetter", {"stadt": "Berlin"}),
        ("call_def", "wetter", {"stadt": "Rom"}),
    ]
    assert payloads[0]["tools"] == to_openai_tools([TOOL])


async def test_stream_tool_call_with_broken_arguments() -> None:
    body = _sse(
        _delta(
            {"tool_calls": [{"index": 0, "function": {"name": "wetter", "arguments": "{kaputt"}}]}
        ),
        _delta({"tool_calls": [{"index": 2, "function": {"name": "uhr"}}]}),
        "[DONE]",
    )
    provider = _provider(lambda request: httpx.Response(200, content=body))
    response = await provider.complete(_request(tools=[TOOL]))
    first, second = response.tool_calls
    assert (first.id, first.arguments) == ("call_0", {"_raw": "{kaputt"})  # ID fehlt → Ersatz
    assert (second.id, second.name, second.arguments) == ("call_2", "uhr", {})


async def test_stream_stops_at_done() -> None:
    body = _sse(_delta({"content": "A"}), "[DONE]", _delta({"content": "B"}))
    provider = _provider(lambda request: httpx.Response(200, content=body))
    assert (await provider.complete(_request())).text == "A"


@pytest.mark.parametrize(
    ("status", "expected", "retry_after"),
    [
        (401, ProviderAuthError, None),
        (403, ProviderAuthError, None),
        (404, ModelNotFoundError, None),
        (429, ProviderUnavailableError, 60),
        (500, ProviderUnavailableError, 0),
        (503, ProviderUnavailableError, 0),
        (400, ProviderRequestError, None),
        (422, ProviderRequestError, None),
    ],
)
async def test_stream_http_errors(
    status: int, expected: type[ProviderError], retry_after: float | None
) -> None:
    body = '{"error": {"message": "nope"}}'
    provider = _provider(lambda request: httpx.Response(status, text=body))
    with pytest.raises(expected) as info:
        await provider.complete(_request())
    error = info.value
    assert type(error) is expected
    assert error.provider == "groq" and error.model == "groq-llama"
    if retry_after is not None:
        assert isinstance(error, ProviderUnavailableError)
        assert error.retry_after_s == retry_after


async def test_stream_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Verbindung abgelehnt", request=request)

    provider = _provider(handler)
    with pytest.raises(ProviderUnavailableError, match="groq nicht erreichbar"):
        await provider.complete(_request())


async def test_stream_invalid_json() -> None:
    provider = _provider(lambda request: httpx.Response(200, content=b"data: {kaputt\n\n"))
    with pytest.raises(ProviderUnavailableError):
        await provider.complete(_request())


async def test_refresh_and_aclose_reset_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY_NAME, "erster-key-123")
    provider = _provider(lambda request: httpx.Response(200), api_key_name=KEY_NAME)
    client = provider.http()
    assert client.headers["Authorization"] == "Bearer erster-key-123"
    monkeypatch.setenv(KEY_NAME, "zweiter-key-456")
    provider.refresh()
    assert provider.http().headers["Authorization"] == "Bearer zweiter-key-456"
    await provider.aclose()
    await provider.aclose()  # zweimal schließen ist harmlos
