"""Tests für das Gemini-Plugin – Umwandlungen und Fehler, ohne Netzwerk (Client wird nachgebaut)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from google.genai import errors as genai_errors
from google.genai import types

from jarvis.core.config import ProviderConfig
from jarvis.router.base import ChatMessage, ChatRequest, ToolCall, ToolSpec
from jarvis.router.errors import (
    ModelNotFoundError,
    ProviderAuthError,
    ProviderError,
    ProviderRequestError,
    ProviderUnavailableError,
)
from jarvis.router.providers.gemini_provider import (
    GeminiProvider,
    map_gemini_error,
    to_contents,
    to_tools,
)
from jarvis.security.secrets import SecretStore

TOOL = ToolSpec(
    name="wetter",
    description="Wetter für eine Stadt",
    parameters={"type": "object", "properties": {"stadt": {"type": "string"}}},
)


def _provider(
    monkeypatch: pytest.MonkeyPatch, *, key: str | None = "gemini-test-key-123"
) -> GeminiProvider:
    if key is None:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    else:
        monkeypatch.setenv("GEMINI_API_KEY", key)
    return GeminiProvider(ProviderConfig(timeout_s=30), SecretStore(use_keyring=False))


def _request(**overrides: Any) -> ChatRequest:
    data: dict[str, Any] = {
        "model": "gemini-flash",
        "provider_model": "gemini-3.8-flash",
        "messages": [ChatMessage(role="user", content="Hallo")],
        "max_tokens": 2048,
    }
    data.update(overrides)
    return ChatRequest(**data)


# ---------------------------------------------------------------- to_contents


def test_to_contents_user_and_assistant_text() -> None:
    contents = to_contents(
        [
            ChatMessage(role="user", content="Hallo"),
            ChatMessage(role="assistant", content="Hallo zurück"),
        ],
        "gemini",
    )
    assert [c.role for c in contents] == ["user", "model"]
    assert contents[0].parts is not None and contents[0].parts[0].text == "Hallo"
    assert contents[1].parts is not None and contents[1].parts[0].text == "Hallo zurück"


def test_to_contents_tool_calls_and_grouped_results() -> None:
    messages = [
        ChatMessage(role="user", content="Wetter in Berlin und Hamburg?"),
        ChatMessage(
            role="assistant",
            content="Ich frage nach.",
            tool_calls=(
                ToolCall(id="c1", name="wetter", arguments={"stadt": "Berlin"}),
                ToolCall(id="c2", name="wetter", arguments={"stadt": "Hamburg"}),
            ),
        ),
        ChatMessage(role="tool", content="Sonnig", tool_call_id="c1", name="wetter"),
        ChatMessage(role="tool", content="Regen", tool_call_id="c2"),
        ChatMessage(role="user", content="Danke"),
    ]
    contents = to_contents(messages, "gemini")
    assert [c.role for c in contents] == ["user", "model", "user", "user"]

    model_parts = contents[1].parts or []
    assert model_parts[0].text == "Ich frage nach."
    calls = [p.function_call for p in model_parts[1:]]
    assert [(c.id, c.name, c.args) for c in calls if c is not None] == [
        ("c1", "wetter", {"stadt": "Berlin"}),
        ("c2", "wetter", {"stadt": "Hamburg"}),
    ]

    # Beide Werkzeug-Ergebnisse landen gebündelt in EINER user-Nachricht.
    responses = [p.function_response for p in contents[2].parts or []]
    assert [(r.id, r.name, r.response) for r in responses if r is not None] == [
        ("c1", "wetter", {"result": "Sonnig"}),
        ("c2", "tool", {"result": "Regen"}),  # Name fehlt → "tool"
    ]
    assert (contents[3].parts or [])[0].text == "Danke"


def test_to_contents_flushes_trailing_tool_results() -> None:
    messages = [
        ChatMessage(role="user", content="Uhrzeit?"),
        ChatMessage(role="assistant", tool_calls=(ToolCall(id="c1", name="uhr", arguments={}),)),
        ChatMessage(role="tool", content="14:30", tool_call_id="c1", name="uhr"),
    ]
    contents = to_contents(messages, "gemini")
    assert [c.role for c in contents] == ["user", "model", "user"]
    # Ohne Text keine leere Text-Part – nur der Funktionsaufruf.
    model_parts = contents[1].parts or []
    assert len(model_parts) == 1 and model_parts[0].text is None
    last = contents[2].parts or []
    assert last[0].function_response is not None
    assert last[0].function_response.response == {"result": "14:30"}


def test_to_contents_skips_empty_assistant_message() -> None:
    contents = to_contents(
        [ChatMessage(role="user", content="a"), ChatMessage(role="assistant", content="")],
        "gemini",
    )
    assert [c.role for c in contents] == ["user"]


def test_to_contents_reuses_original_parts_with_thought_signatures() -> None:
    original = [
        types.Part(text="Nachdenken …", thought=True),
        types.Part(
            function_call=types.FunctionCall(id="c1", name="wetter", args={"stadt": "Berlin"}),
            thought_signature=b"signatur",
        ),
    ]
    message = ChatMessage(
        role="assistant",
        content="",
        tool_calls=(ToolCall(id="c1", name="wetter", arguments={"stadt": "Berlin"}),),
        provider_data={"provider": "gemini", "gemini_parts": original},
    )
    [content] = to_contents([message], "gemini")
    assert content.role == "model"
    assert content.parts == original
    assert content.parts is not None and content.parts[1].thought_signature == b"signatur"


def test_to_contents_ignores_parts_of_other_provider_instance() -> None:
    original = [types.Part(text="alt", thought_signature=b"x")]
    message = ChatMessage(
        role="assistant",
        content="neu",
        provider_data={"provider": "gemini-zweitkonto", "gemini_parts": original},
    )
    [content] = to_contents([message], "gemini")
    parts = content.parts or []
    assert [p.text for p in parts] == ["neu"]
    assert parts[0].thought_signature is None


# ---------------------------------------------------------------- to_tools


def test_to_tools() -> None:
    assert to_tools([]) == []
    other = ToolSpec(name="uhr", description="Uhrzeit", parameters={"type": "object"})
    [tool] = to_tools([TOOL, other])
    declarations = tool.function_declarations or []
    assert [d.name for d in declarations] == ["wetter", "uhr"]
    assert declarations[0].description == "Wetter für eine Stadt"
    assert declarations[0].parameters_json_schema == dict(TOOL.parameters)


# ---------------------------------------------------------------- build_config


def test_build_config_minimal(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _provider(monkeypatch).build_config(_request(system=""))
    assert config.system_instruction is None
    assert config.max_output_tokens == 2048
    assert config.tools is None
    assert config.automatic_function_calling is not None
    assert config.automatic_function_calling.disable is True
    assert config.thinking_config is None


def test_build_config_with_system_tools_and_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request(system="Du bist JARVIS.", tools=[TOOL], effort="low")
    config = _provider(monkeypatch).build_config(request)
    assert config.system_instruction == "Du bist JARVIS."
    assert config.tools is not None and len(config.tools) == 1
    assert config.thinking_config is not None
    assert config.thinking_config.thinking_level == types.ThinkingLevel.LOW


@pytest.mark.parametrize(
    ("effort", "level"),
    [
        ("medium", types.ThinkingLevel.MEDIUM),
        ("high", types.ThinkingLevel.HIGH),
        ("xhigh", types.ThinkingLevel.HIGH),
        ("max", types.ThinkingLevel.HIGH),
    ],
)
def test_build_config_effort_mapping(
    monkeypatch: pytest.MonkeyPatch, effort: str, level: types.ThinkingLevel
) -> None:
    config = _provider(monkeypatch).build_config(_request(effort=effort))
    assert config.thinking_config is not None
    assert config.thinking_config.thinking_level == level


def test_build_config_thinking_level_option_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request(effort="high", options={"thinking_level": "minimal"})
    config = _provider(monkeypatch).build_config(request)
    assert config.thinking_config is not None
    assert config.thinking_config.thinking_level == types.ThinkingLevel.MINIMAL


# ---------------------------------------------------------------- Fehler


def _api_error(code: int, message: str = "Fehler", **extra: Any) -> genai_errors.APIError:
    body: dict[str, Any] = {"error": {"code": code, "message": message, "status": "X", **extra}}
    return genai_errors.APIError(code, body)


def test_map_rate_limit_uses_retry_delay() -> None:
    exc = _api_error(
        429,
        "Quota exceeded",
        details=[{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}],
    )
    error = map_gemini_error(exc, provider="gemini", model="gemini-flash")
    assert isinstance(error, ProviderUnavailableError)
    assert error.retry_after_s == 38  # +1 Sekunde Puffer
    assert "Quota exceeded" in str(error)
    assert error.provider == "gemini" and error.model == "gemini-flash"


def test_map_rate_limit_default_pause() -> None:
    error = map_gemini_error(_api_error(429), provider="gemini", model="m")
    assert isinstance(error, ProviderUnavailableError)
    assert error.retry_after_s == 60.0


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (401, ProviderAuthError),
        (403, ProviderAuthError),
        (404, ModelNotFoundError),
        (500, ProviderUnavailableError),
        (503, ProviderUnavailableError),
        (400, ProviderRequestError),
    ],
)
def test_map_api_errors(code: int, expected: type[ProviderError]) -> None:
    error = map_gemini_error(_api_error(code), provider="gemini", model="m")
    assert type(error) is expected
    if isinstance(error, ProviderUnavailableError):
        assert error.retry_after_s == 0


def test_map_client_error_subclass() -> None:
    exc = genai_errors.ClientError(400, {"error": {"code": 400, "message": "kaputt"}})
    error = map_gemini_error(exc, provider="gemini", model="m")
    assert isinstance(error, ProviderRequestError)
    assert "kaputt" in str(error)


def test_map_network_error() -> None:
    exc = httpx.ConnectError("Verbindung abgelehnt")
    error = map_gemini_error(exc, provider="gemini", model="m")
    assert isinstance(error, ProviderUnavailableError)
    assert "nicht erreichbar" in str(error)


# ---------------------------------------------------------------- Client & Stream


def test_unavailable_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider(monkeypatch, key=None)
    assert provider.unavailable_reason() == "kein API-Key (GEMINI_API_KEY)"
    with pytest.raises(ProviderAuthError):
        provider.client()


def test_client_is_cached_until_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider(monkeypatch)
    client = provider.client()
    assert provider.client() is client
    provider.refresh()
    assert provider.client() is not client


class _FakeModels:
    def __init__(self, chunks: Sequence[types.GenerateContentResponse] | Exception) -> None:
        self.chunks = chunks
        self.calls: list[dict[str, Any]] = []

    async def generate_content_stream(
        self, **kwargs: Any
    ) -> AsyncIterator[types.GenerateContentResponse]:
        self.calls.append(kwargs)
        if isinstance(self.chunks, Exception):
            raise self.chunks
        chunks = list(self.chunks)

        async def iterate() -> AsyncIterator[types.GenerateContentResponse]:
            for chunk in chunks:
                yield chunk

        return iterate()

    async def embed_content(self, **kwargs: Any) -> types.EmbedContentResponse:
        self.calls.append(kwargs)
        if isinstance(self.chunks, Exception):
            raise self.chunks
        return types.EmbedContentResponse(
            embeddings=[
                types.ContentEmbedding(values=[0.1, 0.2]),
                types.ContentEmbedding(values=[0.3, 0.4]),
            ]
        )


def _install(
    monkeypatch: pytest.MonkeyPatch, chunks: Sequence[types.GenerateContentResponse] | Exception
) -> tuple[GeminiProvider, _FakeModels]:
    provider = _provider(monkeypatch)
    models = _FakeModels(chunks)
    provider._client = SimpleNamespace(aio=SimpleNamespace(models=models))  # type: ignore[assignment]
    return provider, models


def _chunk(
    *parts: types.Part,
    finish: types.FinishReason | None = None,
    usage: types.GenerateContentResponseUsageMetadata | None = None,
) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=list(parts)), finish_reason=finish
            )
        ],
        usage_metadata=usage,
    )


async def test_stream_text_tool_call_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    thought = types.Part(text="Ich überlege …", thought=True)
    call = types.Part(
        function_call=types.FunctionCall(name="wetter", args={"stadt": "Berlin"}),
        thought_signature=b"sig",
    )
    chunks = [
        _chunk(thought),
        _chunk(types.Part(text="Moment,")),
        _chunk(types.Part(text=" ich schaue nach."), call),
        _chunk(
            finish=types.FinishReason.STOP,
            usage=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=120, candidates_token_count=30, thoughts_token_count=50
            ),
        ),
    ]
    provider, models = _install(monkeypatch, chunks)
    request = _request(system="Du bist JARVIS.", tools=[TOOL])
    received = [c async for c in provider.stream(request)]

    assert [c.text for c in received[:-1]] == ["Moment,", " ich schaue nach."]  # ohne Gedanken
    response = received[-1].response
    assert response is not None
    assert response.text == "Moment, ich schaue nach."
    assert response.stop_reason == "STOP"
    assert not response.refused
    assert response.model == "gemini-flash" and response.provider == "gemini"
    [tool_call] = response.tool_calls
    assert tool_call.name == "wetter"
    assert tool_call.arguments == {"stadt": "Berlin"}
    assert tool_call.id.startswith("call_")  # Gemini liefert oft keine ID
    [segment] = response.usage
    assert (segment.input_tokens, segment.output_tokens) == (120, 80)  # Gedanken zählen mit

    # Alle Original-Parts (inkl. Gedanken und Signatur) bleiben für den nächsten Schritt erhalten.
    assert response.provider_data is not None
    assert response.provider_data["provider"] == "gemini"
    assert response.provider_data["gemini_parts"] == [
        thought,
        types.Part(text="Moment,"),
        types.Part(text=" ich schaue nach."),
        call,
    ]

    [kwargs] = models.calls
    assert kwargs["model"] == "gemini-3.8-flash"
    assert kwargs["config"].system_instruction == "Du bist JARVIS."
    assert [c.role for c in kwargs["contents"]] == ["user"]


async def test_stream_round_trip_preserves_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    call = types.Part(
        function_call=types.FunctionCall(id="c9", name="wetter", args={}),
        thought_signature=b"sig",
    )
    provider, _models = _install(monkeypatch, [_chunk(call, finish=types.FinishReason.STOP)])
    response = await provider.complete(_request())
    assert response.tool_calls[0].id == "c9"
    follow_up = [
        ChatMessage(role="user", content="Wetter?"),
        ChatMessage(
            role="assistant",
            tool_calls=tuple(response.tool_calls),
            provider_data=response.provider_data,
        ),
        ChatMessage(role="tool", content="Sonnig", tool_call_id="c9", name="wetter"),
    ]
    contents = to_contents(follow_up, provider.instance_name)
    assert contents[1].parts == [call]


async def test_stream_safety_block_is_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [_chunk(finish=types.FinishReason.SAFETY)]
    provider, _ = _install(monkeypatch, chunks)
    response = await provider.complete(_request())
    assert response.refused is True
    assert response.stop_reason == "SAFETY"
    assert response.text == ""
    assert response.usage[0].input_tokens == 0


async def test_stream_maps_api_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, _ = _install(monkeypatch, _api_error(404, "not found"))
    with pytest.raises(ModelNotFoundError, match="gemini-flash") as info:
        await provider.complete(_request())
    assert isinstance(info.value.__cause__, genai_errors.APIError)


async def test_stream_maps_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    exc = _api_error(429, details=[{"retryDelay": "5s"}])
    provider, _ = _install(monkeypatch, exc)
    with pytest.raises(ProviderUnavailableError) as info:
        await provider.complete(_request())
    assert info.value.retry_after_s == 6


async def test_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, models = _install(monkeypatch, [])
    vectors = await provider.embed(["a", "b"])
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert models.calls[0] == {"model": "gemini-embedding-2", "contents": ["a", "b"]}


async def test_embed_maps_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, _ = _install(monkeypatch, _api_error(403))
    with pytest.raises(ProviderAuthError):
        await provider.embed(["a"])
