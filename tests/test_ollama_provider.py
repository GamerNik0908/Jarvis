"""Tests für das Ollama-Plugin – mit ``httpx.MockTransport`` statt eines laufenden Servers."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from jarvis.core.config import ModelConfig, ProviderConfig
from jarvis.router.base import ChatMessage, ChatRequest, ToolCall, ToolSpec
from jarvis.router.errors import (
    ModelNotFoundError,
    ProviderError,
    ProviderRequestError,
    ProviderUnavailableError,
)
from jarvis.router.providers.ollama_provider import (
    OllamaProvider,
    model_matches,
    to_ollama_messages,
    to_ollama_tools,
)
from jarvis.security.secrets import SecretStore

Handler = Callable[[httpx.Request], httpx.Response]

TOOL = ToolSpec(
    name="uhrzeit",
    description="Aktuelle Uhrzeit",
    parameters={"type": "object", "properties": {"zone": {"type": "string"}}},
)
TAGS = {
    "models": [
        {"name": "qwen3:latest"},
        {"name": "llama3.2:3b"},
        {"name": "nomic-embed-text:latest"},
    ]
}


def _provider(handler: Handler, **config: Any) -> OllamaProvider:
    provider = OllamaProvider(
        ProviderConfig(base_url="http://ollama.test:11434/", **config),
        SecretStore(use_keyring=False),
    )
    provider.transport = httpx.MockTransport(handler)
    return provider


def _model(target: str) -> ModelConfig:
    return ModelConfig(id=f"lokal-{target}", provider="ollama", provider_model=target, local=True)


def _ndjson(*lines: dict[str, Any]) -> bytes:
    return "\n".join(json.dumps(line) for line in lines).encode("utf-8") + b"\n"


def _request(**overrides: Any) -> ChatRequest:
    data: dict[str, Any] = {
        "model": "lokal-qwen3",
        "provider_model": "qwen3",
        "messages": [ChatMessage(role="user", content="Hallo")],
        "max_tokens": 512,
    }
    data.update(overrides)
    return ChatRequest(**data)


def _down(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("Verbindung abgelehnt", request=request)


# ---------------------------------------------------------------- Umwandlungen


@pytest.mark.parametrize(
    ("wanted", "expected"),
    [
        ("qwen3", True),  # passt zu qwen3:latest
        ("qwen3:latest", True),
        ("llama3.2:3b", True),
        ("llama3.2", False),  # nur :latest wird ergänzt
        ("qwen3:8b", False),
        ("mistral", False),
    ],
)
def test_model_matches(wanted: str, expected: bool) -> None:
    installed = {"qwen3:latest", "llama3.2:3b"}
    assert model_matches(installed, wanted) is expected


def test_to_ollama_messages() -> None:
    messages = [
        ChatMessage(role="user", content="Wie spät?"),
        ChatMessage(
            role="assistant",
            tool_calls=(ToolCall(id="c1", name="uhrzeit", arguments={"zone": "Berlin"}),),
        ),
        ChatMessage(role="tool", content="14:30", tool_call_id="c1", name="uhrzeit"),
        ChatMessage(role="tool", content="?", tool_call_id="c2"),
    ]
    assert to_ollama_messages(messages, "Du bist JARVIS.") == [
        {"role": "system", "content": "Du bist JARVIS."},
        {"role": "user", "content": "Wie spät?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "uhrzeit", "arguments": {"zone": "Berlin"}}}],
        },
        {"role": "tool", "content": "14:30", "tool_name": "uhrzeit"},
        {"role": "tool", "content": "?", "tool_name": "tool"},
    ]
    assert to_ollama_messages([ChatMessage(role="user", content="x")], None) == [
        {"role": "user", "content": "x"}
    ]


def test_to_ollama_tools() -> None:
    assert to_ollama_tools([TOOL]) == [
        {
            "type": "function",
            "function": {
                "name": "uhrzeit",
                "description": "Aktuelle Uhrzeit",
                "parameters": dict(TOOL.parameters),
            },
        }
    ]


# ---------------------------------------------------------------- Probe


async def test_probe_lists_installed_models() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=TAGS)

    provider = _provider(handler)
    # Vor der Prüfung: Versuch erlaubt.
    assert provider.model_unavailable_reason(_model("mistral")) is None
    await provider.probe()
    assert seen == ["http://ollama.test:11434/api/tags"]
    assert provider.installed_models == {"qwen3:latest", "llama3.2:3b", "nomic-embed-text:latest"}
    assert provider.unavailable_reason() is None
    assert not provider.probe_stale
    assert provider.model_unavailable_reason(_model("qwen3")) is None
    assert provider.model_unavailable_reason(_model("mistral")) == (
        "nicht installiert – 'ollama pull mistral'"
    )
    await provider.aclose()


async def test_probe_when_server_is_down() -> None:
    provider = _provider(_down)
    await provider.probe()
    assert "Ollama läuft nicht" in (provider.unavailable_reason() or "")
    assert provider.installed_models == set()
    assert provider.model_unavailable_reason(_model("qwen3")) is None


@pytest.mark.parametrize(
    "response",
    [httpx.Response(500, text="interner Fehler"), httpx.Response(200, text="<html>kein json")],
)
async def test_probe_with_broken_answer(response: httpx.Response) -> None:
    provider = _provider(lambda request: response)
    await provider.probe()
    assert provider.unavailable_reason() is not None


async def test_probe_recovers_after_restart() -> None:
    state = {"up": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if not state["up"]:
            raise httpx.ConnectError("aus", request=request)
        return httpx.Response(200, json=TAGS)

    provider = _provider(handler)
    await provider.probe()
    assert provider.unavailable_reason() is not None
    state["up"] = True
    await provider.probe()
    assert provider.unavailable_reason() is None


def test_disabled_provider() -> None:
    provider = _provider(_down, enabled=False)
    assert provider.unavailable_reason() == "Anbieter deaktiviert"


# ---------------------------------------------------------------- Chat-Stream


async def test_stream_text_tool_call_and_usage() -> None:
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/api/chat"
        payloads.append(json.loads(request.content))
        body = _ndjson(
            {"message": {"role": "assistant", "content": "Es ist"}, "done": False},
            {"message": {"role": "assistant", "content": " gleich"}, "done": False},
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "uhrzeit", "arguments": {"zone": "Berlin"}}}
                    ],
                },
                "done": False,
            },
            {
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 42,
                "eval_count": 7,
            },
        )
        # Leerzeilen zwischen den Objekten werden übersprungen.
        return httpx.Response(200, content=body.replace(b"\n", b"\n\n", 1))

    provider = _provider(handler)
    request = _request(
        system="Du bist JARVIS.",
        tools=[TOOL],
        options={"temperature": 0.2, "num_ctx": 16384, "think": True, "keep_alive": "1h"},
    )
    chunks = [c async for c in provider.stream(request)]
    assert [c.text for c in chunks[:-1]] == ["Es ist", " gleich"]
    response = chunks[-1].response
    assert response is not None
    assert response.text == "Es ist gleich"
    assert response.stop_reason == "stop"
    assert response.provider == "ollama" and response.model == "lokal-qwen3"
    [call] = response.tool_calls
    assert call.name == "uhrzeit"
    assert call.arguments == {"zone": "Berlin"}
    assert call.id.startswith("call_")
    [segment] = response.usage
    assert (segment.model, segment.input_tokens, segment.output_tokens) == ("lokal-qwen3", 42, 7)

    [payload] = payloads
    assert payload["model"] == "qwen3"
    assert payload["stream"] is True
    assert payload["think"] is True
    assert payload["keep_alive"] == "1h"
    assert payload["options"] == {"num_predict": 512, "num_ctx": 16384, "temperature": 0.2}
    assert payload["messages"][0] == {"role": "system", "content": "Du bist JARVIS."}
    assert payload["tools"] == to_ollama_tools([TOOL])


async def test_stream_defaults_and_string_arguments() -> None:
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=_ndjson(
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_7",
                                "function": {"name": "uhrzeit", "arguments": '{"zone": "UTC"}'},
                            }
                        ],
                    },
                    "done": True,
                }
            ),
        )

    provider = _provider(handler)
    response = await provider.complete(_request())
    [call] = response.tool_calls
    assert (call.id, call.arguments) == ("call_7", {"zone": "UTC"})
    assert response.stop_reason == "stop"
    assert response.input_tokens == 0 and response.output_tokens == 0
    [payload] = payloads
    assert payload["think"] is False
    assert payload["keep_alive"] == "15m"
    assert payload["options"] == {"num_predict": 512, "num_ctx": 8192}
    assert "tools" not in payload
    assert [m["role"] for m in payload["messages"]] == ["user"]


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (404, '{"error":"model \\"qwen3\\" not found, try pulling it first"}', ModelNotFoundError),
        (
            400,
            '{"error":"registry.ollama.ai/library/gemma3 does not support tools"}',
            ModelNotFoundError,
        ),
        (500, '{"error":"llama runner process has terminated"}', ProviderUnavailableError),
        (400, '{"error":"invalid options"}', ProviderRequestError),
    ],
)
async def test_stream_http_errors(status: int, body: str, expected: type[ProviderError]) -> None:
    provider = _provider(lambda request: httpx.Response(status, text=body))
    with pytest.raises(expected) as info:
        await provider.complete(_request())
    assert type(info.value) is expected
    assert info.value.provider == "ollama"
    assert info.value.model == "lokal-qwen3"


async def test_missing_model_error_suggests_pull() -> None:
    # Der Hinweis nennt den Ollama-Namen, nicht die Katalog-ID ("lokal-qwen3").
    provider = _provider(lambda request: httpx.Response(404, text="not found"))
    with pytest.raises(ModelNotFoundError, match="'ollama pull qwen3'") as info:
        await provider.complete(_request())
    assert info.value.model == "lokal-qwen3"
    body = _ndjson({"error": "model 'qwen3' not found"})
    provider = _provider(lambda request: httpx.Response(200, content=body))
    with pytest.raises(ModelNotFoundError, match="'ollama pull qwen3'"):
        await provider.complete(_request())


async def test_stream_error_line_after_start() -> None:
    body = _ndjson(
        {"message": {"content": "Hal"}, "done": False},
        {"error": "model 'qwen3' not found"},
    )
    provider = _provider(lambda request: httpx.Response(200, content=body))
    received: list[str] = []
    with pytest.raises(ModelNotFoundError):
        async for chunk in provider.stream(_request()):
            received.append(chunk.text)
    assert received == ["Hal"]


async def test_stream_error_line_server_side() -> None:
    body = _ndjson({"error": "CUDA out of memory"})
    provider = _provider(lambda request: httpx.Response(200, content=body))
    with pytest.raises(ProviderUnavailableError, match="CUDA"):
        await provider.complete(_request())


async def test_stream_invalid_json_line() -> None:
    provider = _provider(lambda request: httpx.Response(200, content=b"{kaputt\n"))
    with pytest.raises(ProviderUnavailableError, match="Ollama-Fehler"):
        await provider.complete(_request())


async def test_stream_when_server_is_down_marks_unreachable() -> None:
    provider = _provider(_down)
    assert provider.unavailable_reason() is None
    with pytest.raises(ProviderUnavailableError, match="nicht erreichbar"):
        await provider.complete(_request())
    assert "Ollama läuft nicht" in (provider.unavailable_reason() or "")


async def test_stream_timeout_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("zu langsam", request=request)

    provider = _provider(handler)
    with pytest.raises(ProviderUnavailableError, match="Ollama-Fehler"):
        await provider.complete(_request())
    assert provider.unavailable_reason() is None  # Zeitüberschreitung ≠ Server aus


# ---------------------------------------------------------------- Embeddings & Pull


async def test_embed() -> None:
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/embed"
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"embeddings": [[1, 2.5], [0, -1]]})

    provider = _provider(handler)
    vectors = await provider.embed(["a", "b"], "nomic-embed-text")
    assert vectors == [[1.0, 2.5], [0.0, -1.0]]
    assert all(isinstance(v, float) for vector in vectors for v in vector)
    assert payloads == [{"model": "nomic-embed-text", "input": ["a", "b"]}]


async def test_embed_missing_model() -> None:
    provider = _provider(lambda request: httpx.Response(404, text='{"error":"not found"}'))
    with pytest.raises(ModelNotFoundError):
        await provider.embed(["a"], "nomic-embed-text")


async def test_pull_reports_progress() -> None:
    body = _ndjson(
        {"status": "pulling manifest"},
        {"status": "pulling abc", "total": 100, "completed": 50},
        {"status": "success"},
    )
    provider = _provider(lambda request: httpx.Response(200, content=body))
    progress = [p async for p in provider.pull("qwen3")]
    assert [p["status"] for p in progress] == ["pulling manifest", "pulling abc", "success"]


async def test_aclose_resets_client() -> None:
    provider = _provider(lambda request: httpx.Response(200, json=TAGS))
    client = provider.http()
    assert provider.http() is client
    await provider.aclose()
    assert client.is_closed
    assert provider.http() is not client
    await provider.aclose()
