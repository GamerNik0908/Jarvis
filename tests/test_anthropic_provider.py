"""Tests für das Anthropic-Plugin – ohne Netzwerk, mit nachgebauten SDK-Objekten."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from jarvis.core.config import ProviderConfig
from jarvis.router.base import ChatMessage, ChatRequest
from jarvis.router.errors import (
    ModelNotFoundError,
    ProviderAuthError,
    ProviderRequestError,
    ProviderUnavailableError,
)
from jarvis.router.providers.anthropic_provider import (
    REFUSAL_FALLBACK_BETA,
    AnthropicProvider,
    map_anthropic_error,
)
from jarvis.security.secrets import SecretStore

REQUEST = ChatRequest(
    model="claude-opus-5-5",
    messages=[ChatMessage(role="user", content="Hallo")],
    max_tokens=1000,
    system="Du bist JARVIS.",
    effort="low",
    refusal_fallback=True,
)


def _provider(monkeypatch: pytest.MonkeyPatch, **options: Any) -> AnthropicProvider:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-1234567890")
    return AnthropicProvider(ProviderConfig(options=options), SecretStore(use_keyring=False))


def _message(**overrides: Any) -> SimpleNamespace:
    data: dict[str, Any] = {
        "model": "claude-opus-5-5",
        "content": [
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text="Hallo"),
            SimpleNamespace(type="text", text=" zurück"),
        ],
        "stop_reason": "end_turn",
        "stop_details": None,
        "usage": SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            cache_read_input_tokens=50,
            cache_creation_input_tokens=None,
            iterations=None,
        ),
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_build_params(monkeypatch: pytest.MonkeyPatch) -> None:
    params = _provider(monkeypatch).build_params(REQUEST)
    assert params["model"] == "claude-opus-5-5"
    assert params["system"] == "Du bist JARVIS."
    assert params["messages"] == [{"role": "user", "content": "Hallo"}]
    assert params["output_config"] == {"effort": "low"}
    assert params["cache_control"] == {"type": "ephemeral"}
    assert params["fallbacks"] == "default"
    assert params["betas"] == [REFUSAL_FALLBACK_BETA]
    assert "temperature" not in params


def test_build_params_minimal(monkeypatch: pytest.MonkeyPatch) -> None:
    request = ChatRequest(model="claude-haiku-5-5", messages=REQUEST.messages, max_tokens=10)
    params = _provider(monkeypatch, prompt_caching=False).build_params(request)
    assert set(params) == {"model", "max_tokens", "messages"}


def test_unavailable_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    provider = AnthropicProvider(ProviderConfig(), SecretStore(use_keyring=False))
    assert provider.unavailable_reason() == "kein API-Key (ANTHROPIC_API_KEY)"


def test_to_response_simple(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _provider(monkeypatch).to_response(_message(), REQUEST)
    assert response.text == "Hallo zurück"
    assert response.model == "claude-opus-5-5"
    assert response.usage[0].input_tokens == 100
    assert response.usage[0].cache_read_tokens == 50
    assert response.usage[0].cache_write_tokens == 0
    assert not response.refused and not response.provider_fallback_used


def test_to_response_with_server_side_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=30,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        iterations=[
            SimpleNamespace(
                type="message",
                model="claude-opus-5-5",
                input_tokens=100,
                output_tokens=0,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
            SimpleNamespace(
                type="fallback_message",
                model="claude-opus-4-8",
                input_tokens=100,
                output_tokens=30,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        ],
    )
    message = _message(model="claude-opus-4-8", usage=usage)
    response = _provider(monkeypatch).to_response(message, REQUEST)
    assert response.provider_fallback_used
    assert response.model == "claude-opus-4-8"
    assert [s.model for s in response.usage] == ["claude-opus-5-5", "claude-opus-4-8"]


def test_to_response_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    message = _message(
        content=[],
        stop_reason="refusal",
        stop_details=SimpleNamespace(type="refusal", category="cyber", explanation=None),
    )
    response = _provider(monkeypatch).to_response(message, REQUEST)
    assert response.refused and response.refusal_category == "cyber"
    assert response.text == ""


def _status_error(cls: type[anthropic.APIStatusError], status: int) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("fehler", response=httpx2.Response(status, request=request), body=None)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (lambda: _status_error(anthropic.AuthenticationError, 401), ProviderAuthError),
        (lambda: _status_error(anthropic.PermissionDeniedError, 403), ProviderAuthError),
        (lambda: _status_error(anthropic.NotFoundError, 404), ModelNotFoundError),
        (lambda: _status_error(anthropic.RateLimitError, 429), ProviderUnavailableError),
        (lambda: _status_error(anthropic.InternalServerError, 500), ProviderUnavailableError),
        (lambda: _status_error(anthropic.APIStatusError, 529), ProviderUnavailableError),
        (lambda: _status_error(anthropic.APIStatusError, 402), ProviderAuthError),
        (lambda: _status_error(anthropic.BadRequestError, 400), ProviderRequestError),
        (
            lambda: anthropic.APIConnectionError(
                request=httpx2.Request("POST", "https://api.anthropic.com")
            ),
            ProviderUnavailableError,
        ),
    ],
)
def test_error_mapping(exc: Any, expected: type[Exception]) -> None:
    mapped = map_anthropic_error(exc(), model="claude-opus-5-5")
    assert type(mapped) is expected
    assert mapped.provider == "anthropic"


class _FakeStream:
    def __init__(self, texts: list[str], final: Any, error: Exception | None = None) -> None:
        self._texts = texts
        self._final = final
        self._error = error

    async def __aenter__(self) -> _FakeStream:
        if self._error is not None:
            raise self._error
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    @property
    def text_stream(self) -> AsyncIterator[str]:
        async def generate() -> AsyncIterator[str]:
            for text in self._texts:
                yield text

        return generate()

    async def get_final_message(self) -> Any:
        return self._final


class _FakeClient:
    def __init__(self, stream: _FakeStream) -> None:
        self.calls: list[dict[str, Any]] = []

        def make_stream(**params: Any) -> _FakeStream:
            self.calls.append(params)
            return stream

        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=make_stream))

    async def close(self) -> None:
        return None


async def test_stream_yields_text_then_response(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider(monkeypatch)
    client = _FakeClient(_FakeStream(["Hal", "lo"], _message()))
    provider._client = client  # type: ignore[assignment]
    chunks = [chunk async for chunk in provider.stream(REQUEST)]
    assert [c.text for c in chunks[:-1]] == ["Hal", "lo"]
    assert chunks[-1].response is not None
    assert chunks[-1].response.text == "Hallo zurück"
    assert client.calls[0]["fallbacks"] == "default"


async def test_stream_maps_sdk_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider(monkeypatch)
    error = _status_error(anthropic.RateLimitError, 429)
    provider._client = _FakeClient(_FakeStream([], None, error=error))  # type: ignore[assignment]
    with pytest.raises(ProviderUnavailableError, match="Rate-Limit"):
        async for _ in provider.stream(REQUEST):
            pass


_SSE_EVENTS = [
    {
        "type": "message_start",
        "message": {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 1},
        },
    },
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hallo"}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " Nik"}},
    {"type": "content_block_stop", "index": 0},
    {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 7},
    },
    {"type": "message_stop"},
]


async def test_real_sdk_against_mock_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Echter SDK-Codepfad (Serialisierung, Header, SSE-Parsing) ohne Netzwerk."""
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in _SSE_EVENTS)
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=body.encode()
        )

    provider = _provider(monkeypatch)
    provider._client = anthropic.AsyncAnthropic(
        api_key="sk-ant-test-key-1234567890",
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
        max_retries=0,
    )
    chunks = [chunk async for chunk in provider.stream(REQUEST)]

    assert "".join(c.text for c in chunks) == "Hallo Nik"
    response = chunks[-1].response
    assert response is not None
    assert response.text == "Hallo Nik"
    assert response.usage[0].input_tokens == 12
    assert response.usage[0].output_tokens == 7

    body = seen["body"]
    assert seen["url"].startswith("https://api.anthropic.com/v1/messages")
    assert REFUSAL_FALLBACK_BETA in seen["headers"]["anthropic-beta"]
    assert body["stream"] is True
    assert body["fallbacks"] == "default"
    assert body["output_config"] == {"effort": "low"}
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["system"] == "Du bist JARVIS."
    assert "temperature" not in body
    await provider.aclose()
