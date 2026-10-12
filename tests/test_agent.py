"""Agent-Loop: Werkzeug-Aufrufe, Ereignisse, Schrittlimit, Fehler und private Daten."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from jarvis.core.agent import _FINAL_NUDGE, TOOL_GUIDE, AgentEvent, merge_roles
from jarvis.core.app import JarvisApp
from jarvis.core.types import TaskKind
from jarvis.plugins.base import Tool, ToolContext, ToolResult, obj
from jarvis.router.base import ChatMessage, ChatResponse, ToolCall, UsageSegment
from jarvis.router.errors import ProviderRequestError
from jarvis.security.killswitch import KillSwitchEngaged

from .conftest import FakeProvider


@pytest.fixture
async def app(config_file: Path) -> AsyncIterator[JarvisApp]:
    application = JarvisApp.bootstrap(config_file, role="test", detect=False)
    yield application
    await application.aclose()


@pytest.fixture
def fake(app: JarvisApp) -> FakeProvider:
    provider = app.router.providers["fake"]
    assert isinstance(provider, FakeProvider)
    return provider


@pytest.fixture
def echo_calls(app: JarvisApp) -> list[dict[str, Any]]:
    """Registriert ein harmloses Test-Werkzeug ``test_echo`` und merkt sich die Aufrufe."""
    calls: list[dict[str, Any]] = []

    async def echo(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        calls.append({"args": args, "session": ctx.session_id, "source": ctx.source})
        return ToolResult(f"Echo: {args['text']}")

    async def secret(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult("Kontostand: 4711 €", sensitive=True)

    app.tools.register(
        Tool(
            name="test_echo",
            description="Wiederholt den Text.",
            parameters=obj({"text": {"type": "string"}}, ["text"]),
            handler=echo,
            category="test",
        )
    )
    app.tools.register(
        Tool(
            name="test_secret",
            description="Liefert private Daten.",
            parameters=obj({}),
            handler=secret,
            category="test",
        )
    )
    return calls


def tool_response(model: str, *calls: ToolCall, text: str = "") -> ChatResponse:
    return ChatResponse(
        text=text,
        provider="fake",
        model=model,
        requested_model=model,
        stop_reason="tool_use",
        usage=[UsageSegment(model=model, input_tokens=100, output_tokens=20)],
        tool_calls=list(calls),
    )


def text_response(model: str, text: str) -> ChatResponse:
    return ChatResponse(
        text=text,
        provider="fake",
        model=model,
        requested_model=model,
        stop_reason="end_turn",
        usage=[UsageSegment(model=model, input_tokens=100, output_tokens=20)],
    )


async def collect(app: JarvisApp, conversation: str, text: str, **kwargs: Any) -> list[AgentEvent]:
    return [event async for event in app.agent.run(conversation, text, **kwargs)]


async def test_tool_call_is_executed_and_result_fed_back(
    app: JarvisApp, fake: FakeProvider, echo_calls: list[dict[str, Any]]
) -> None:
    fake.scripted["fake-big"] = [
        tool_response("fake-big", ToolCall("c1", "test_echo", {"text": "hi"}), text="Moment."),
        text_response("fake-big", "Fertig."),
    ]
    conversation = app.conversations.create()
    events = await collect(app, conversation, "Sag hi")

    assert [e.kind for e in events] == ["text", "tool", "text", "final"]
    assert events[1].text == "test_echo"
    assert events[1].data == {"arguments": {"text": "hi"}}
    final = events[-1]
    assert final.text == "Moment.\n\nFertig."
    assert final.response is not None and final.response.model == "fake-big"
    assert final.data["tools"] == [{"name": "test_echo", "ok": True, "preview": "Echo: hi"}]

    assert echo_calls == [{"args": {"text": "hi"}, "session": conversation, "source": "chat"}]
    assert len(fake.requests) == 2
    assert any(spec.name == "test_echo" for spec in fake.requests[0].tools)
    second = fake.requests[1].messages
    assistant = next(m for m in second if m.role == "assistant")
    assert assistant.tool_calls == (ToolCall("c1", "test_echo", {"text": "hi"}),)
    tool_msg = second[-1]
    assert tool_msg.role == "tool"
    assert tool_msg.content == "Echo: hi"
    assert tool_msg.tool_call_id == "c1" and tool_msg.name == "test_echo"


async def test_ask_persists_conversation_and_uses_history(
    app: JarvisApp, fake: FakeProvider
) -> None:
    conversation = app.conversations.create()
    first = await app.agent.ask(conversation, "Erste Frage")
    assert first.error is None
    assert first.text == "Hallo Welt"
    assert first.tools == [] and first.images == []
    assert first.response is not None and first.response.cost_usd > 0

    stored = app.conversations.messages(conversation)
    assert [(m.role, m.content) for m in stored] == [
        ("user", "Erste Frage"),
        ("assistant", "Hallo Welt"),
    ]
    assert stored[1].meta["model"] == "fake-big"

    await app.agent.ask(conversation, "Zweite Frage")
    contents = [(m.role, m.content) for m in fake.requests[-1].messages]
    assert contents == [
        ("user", "Erste Frage"),
        ("assistant", "Hallo Welt"),
        ("user", "Zweite Frage"),
    ]


async def test_persist_false_stores_nothing(app: JarvisApp, fake: FakeProvider) -> None:
    conversation = app.conversations.create()
    result = await app.agent.ask(conversation, "Nur so", persist=False)
    assert result.text == "Hallo Welt"
    assert app.conversations.messages(conversation) == []


async def test_failed_tool_calls_are_reported_to_the_model(
    app: JarvisApp, fake: FakeProvider, echo_calls: list[dict[str, Any]]
) -> None:
    fake.scripted["fake-big"] = [
        tool_response(
            "fake-big",
            ToolCall("a", "gibt_es_nicht", {}),
            ToolCall("b", "test_echo", {}),  # Pflichtfeld fehlt
        ),
        text_response("fake-big", "Das ging leider schief."),
    ]
    result = await app.agent.ask(app.conversations.create(), "Mach was")

    assert result.text == "Das ging leider schief."
    assert [(t["name"], t["ok"]) for t in result.tools] == [
        ("gibt_es_nicht", False),
        ("test_echo", False),
    ]
    assert echo_calls == []
    tool_messages = [m for m in fake.requests[1].messages if m.role == "tool"]
    assert [m.tool_call_id for m in tool_messages] == ["a", "b"]
    assert tool_messages[0].content == "FEHLER: Unbekanntes Werkzeug 'gibt_es_nicht'"
    assert tool_messages[1].content.startswith("FEHLER: Ungültige Parameter für test_echo")


async def test_max_steps_forces_final_answer_without_tools(
    app: JarvisApp, fake: FakeProvider, echo_calls: list[dict[str, Any]]
) -> None:
    app.config.agent.max_steps = 2
    fake.scripted["fake-big"] = [
        tool_response("fake-big", ToolCall(f"c{i}", "test_echo", {"text": str(i)}))
        for i in range(5)
    ]
    # Nach zwei Werkzeug-Schritten muss der Agent ohne Werkzeuge abschließen.
    fake.scripted["fake-big"].insert(2, text_response("fake-big", "Zwischenstand."))
    result = await app.agent.ask(app.conversations.create(), "Endlos")

    assert len(echo_calls) == 2
    assert len(fake.requests) == 3
    assert fake.requests[0].tools and fake.requests[1].tools
    assert list(fake.requests[2].tools) == []
    assert fake.requests[2].messages[-1] == ChatMessage(role="user", content=_FINAL_NUDGE)
    assert result.text == "Zwischenstand."
    assert result.response is not None


async def test_tool_calls_on_last_step_are_not_executed(
    app: JarvisApp, fake: FakeProvider, echo_calls: list[dict[str, Any]]
) -> None:
    """Hält sich ein Modell nicht an das Schrittlimit, wird nichts mehr ausgeführt."""
    app.config.agent.max_steps = 1
    fake.scripted["fake-big"] = [
        tool_response("fake-big", ToolCall("c1", "test_echo", {"text": "1"})),
        tool_response("fake-big", ToolCall("c2", "test_echo", {"text": "2"}), text="Stand: 1"),
    ]
    result = await app.agent.ask(app.conversations.create(), "Endlos")

    assert [c["args"]["text"] for c in echo_calls] == ["1"]
    assert len(fake.requests) == 2
    assert result.text == "Stand: 1"
    assert result.response is not None and result.response.model == "fake-big"


async def test_provider_error_becomes_agent_error(app: JarvisApp, fake: FakeProvider) -> None:
    fake.behaviours["fake-big"] = ProviderRequestError(
        "Anfrage ungültig", provider="fake", model="fake-big"
    )
    conversation = app.conversations.create()
    events = await collect(app, conversation, "Hallo?")

    assert [e.kind for e in events] == ["error"]
    assert "Anfrage ungültig" in events[0].text
    result = await app.agent.ask(conversation, "Nochmal?")
    assert result.error is not None and "Anfrage ungültig" in result.error
    assert result.text == "" and result.response is None
    roles = [m.role for m in app.conversations.messages(conversation)]
    assert roles == ["user", "error", "user", "error"]
    assert app.events.recent(prefix="chat.error")


async def test_falls_back_to_answer_without_tools_when_no_model_supports_them(
    app: JarvisApp, fake: FakeProvider, echo_calls: list[dict[str, Any]]
) -> None:
    for model_id in ("fake-big", "fake-small"):
        model = app.router.model(model_id)
        assert model is not None
        app.router.replace_model(model.model_copy(update={"supports_tools": False}))

    result = await app.agent.ask(app.conversations.create(), "Hallo")

    assert result.error is None
    assert result.text == "Hallo Welt"
    assert len(fake.requests) == 1
    request = fake.requests[0]
    assert request.model == "fake-big"
    assert list(request.tools) == []
    assert request.system is not None and TOOL_GUIDE not in request.system


async def test_unknown_pinned_model_is_reported_as_error(app: JarvisApp) -> None:
    result = await app.agent.ask(app.conversations.create(), "Hallo", pinned_model="nope")
    assert result.error is not None
    assert "unbekanntes Modell 'nope'" in result.error


async def test_sensitive_tool_result_switches_to_trusted_models(
    app: JarvisApp, fake: FakeProvider, echo_calls: list[dict[str, Any]]
) -> None:
    free = app.router.model("fake-free")
    assert free is not None
    app.router.replace_model(free.model_copy(update={"supports_tools": True}))
    fake.scripted["fake-small"] = [tool_response("fake-small", ToolCall("s", "test_secret", {}))]
    conversation = app.conversations.create()

    result = await app.agent.ask(conversation, "Wie ist mein Kontostand?", task=TaskKind.SIMPLE)

    assert [r.model for r in fake.requests] == ["fake-small", "fake-free"]
    assert "Kontostand: 4711 €" in fake.requests[1].messages[-1].content
    assert result.response is not None and result.response.model == "fake-free"
    assert app.conversations.is_sensitive(conversation)

    # Folgefragen in derselben Unterhaltung bleiben bei vertrauenswürdigen Modellen.
    await app.agent.ask(conversation, "Und jetzt?", task=TaskKind.SIMPLE)
    assert fake.requests[-1].model == "fake-free"


async def test_sensitive_data_never_reaches_untrusted_models(
    app: JarvisApp, fake: FakeProvider, echo_calls: list[dict[str, Any]]
) -> None:
    fake.scripted["fake-big"] = [tool_response("fake-big", ToolCall("s", "test_secret", {}))]
    result = await app.agent.ask(app.conversations.create(), "Kontostand?")

    assert len(fake.requests) == 1  # Route "chat" hat kein vertrauenswürdiges Modell
    assert result.error is not None
    assert "nicht für private Daten freigegeben" in result.error


async def test_tool_names_limit_the_offered_tools(
    app: JarvisApp, fake: FakeProvider, echo_calls: list[dict[str, Any]]
) -> None:
    await app.agent.ask(app.conversations.create(), "x", tool_names=["test_echo"])
    assert [spec.name for spec in fake.requests[0].tools] == ["test_echo"]

    await app.agent.ask(app.conversations.create(), "y", use_tools=False)
    request = fake.requests[1]
    assert list(request.tools) == []
    assert request.system is not None and TOOL_GUIDE not in request.system


async def test_system_prompt_contains_voice_guide_and_extra(
    app: JarvisApp, fake: FakeProvider
) -> None:
    await app.agent.ask(
        app.conversations.create(), "Wie spät?", source="voice", system_extra="ZUSATZ-123"
    )
    system = fake.requests[0].system or ""
    assert "vorgelesen" in system
    assert "ZUSATZ-123" in system
    assert "Aktuelle Zeit:" in system


async def test_events_are_published_on_the_bus(
    app: JarvisApp, fake: FakeProvider, echo_calls: list[dict[str, Any]]
) -> None:
    fake.scripted["fake-big"] = [
        tool_response("fake-big", ToolCall("c1", "test_echo", {"text": "x"})),
        text_response("fake-big", "Ok."),
    ]
    conversation = app.conversations.create()
    await app.agent.ask(conversation, "Los")
    types = [e.type for e in app.events.recent(100) if e.session_id == conversation]
    for expected in ("chat.started", "tool.started", "tool.finished", "chat.delta", "chat.final"):
        assert expected in types
    assert types.index("chat.started") < types.index("tool.started") < types.index("chat.final")


async def test_killswitch_blocks_new_runs(app: JarvisApp) -> None:
    app.killswitch.engage("Test")
    with pytest.raises(KillSwitchEngaged):
        await app.agent.ask(app.conversations.create(), "Hallo")


def test_merge_roles_joins_neighbours_and_starts_with_user() -> None:
    merged = merge_roles(
        [
            ChatMessage(role="assistant", content="Willkommen"),
            ChatMessage(role="user", content="a"),
            ChatMessage(role="user", content="b"),
            ChatMessage(role="assistant", content="c"),
            ChatMessage(role="tool", content="t1", tool_call_id="1"),
            ChatMessage(role="tool", content="t2", tool_call_id="2"),
        ]
    )
    assert [(m.role, m.content) for m in merged] == [
        ("user", "a\n\nb"),
        ("assistant", "c"),
        ("tool", "t1"),
        ("tool", "t2"),
    ]
