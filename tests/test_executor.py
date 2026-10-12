"""Werkzeug-System: Registry, Parameterprüfung und Ausführung über Not-Aus, Stufen und Protokoll."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from jarvis.core.config import AgentConfig, SecurityConfig
from jarvis.core.db import Database
from jarvis.core.events import EventBus
from jarvis.plugins import base
from jarvis.plugins.base import (
    ImageRef,
    Tool,
    ToolContext,
    ToolRegistry,
    ToolResult,
    load_plugins,
    obj,
    tool,
)
from jarvis.plugins.executor import ToolExecutor, coerce_arguments
from jarvis.security.audit import AuditLog
from jarvis.security.killswitch import KillSwitch
from jarvis.security.permissions import ConfirmationBroker, Level
from jarvis.security.redaction import REDACTED

if TYPE_CHECKING:
    from jarvis.core.app import JarvisApp


@dataclass
class Harness:
    """Executor mit echten Sicherheitsbausteinen, aber ohne komplette App."""

    executor: ToolExecutor
    registry: ToolRegistry
    broker: ConfirmationBroker
    killswitch: KillSwitch
    events: EventBus
    audit: AuditLog
    ctx: ToolContext
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def add(self, name: str, level: Any = Level.AUTO, **kwargs: Any) -> Tool:
        """Registriert ein Werkzeug, das seine Aufrufe mitschreibt."""
        result = kwargs.pop("result", None)

        async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
            self.calls.append((name, args))
            return result or ToolResult(f"{name} ok")

        item = Tool(
            name=name,
            description=f"Test-Werkzeug {name}",
            parameters=kwargs.pop("parameters", obj({"path": {"type": "string"}})),
            handler=kwargs.pop("handler", handler),
            level=level,
            **kwargs,
        )
        self.registry.register(item)
        return item

    def audit_rows(self) -> list[dict[str, Any]]:
        return list(reversed(self.audit.recent()))  # älteste zuerst


def make_harness(
    db: Database,
    *,
    tool_levels: dict[str, str] | None = None,
    confirm_timeout_s: float = 5,
    max_chars: int = 12_000,
) -> Harness:
    events = EventBus()
    registry = ToolRegistry()
    broker = ConfirmationBroker(events, timeout_s=confirm_timeout_s)
    audit = AuditLog(db)
    killswitch = KillSwitch(events)
    executor = ToolExecutor(
        registry,
        broker=broker,
        audit=audit,
        killswitch=killswitch,
        events=events,
        security=SecurityConfig.model_validate({"tool_levels": tool_levels or {}}),
        agent=AgentConfig(tool_result_max_chars=max_chars),
    )
    app = cast("JarvisApp", SimpleNamespace(name="fake-app"))
    ctx = ToolContext(app=app, session_id="sitzung-1", source="test")
    return Harness(executor, registry, broker, killswitch, events, audit, ctx)


@pytest.fixture
def h(db: Database) -> Harness:
    return make_harness(db)


async def _pending(broker: ConfirmationBroker) -> dict[str, Any]:
    for _ in range(400):
        latest = broker.latest()
        if latest is not None:
            return latest
        await asyncio.sleep(0.005)
    raise AssertionError("Keine Bestätigungsanfrage gestellt")


# ------------------------------------------------------------------ Registry und Grundlagen


async def _noop(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    return ToolResult("ok")


def test_registry_basics(caplog: pytest.LogCaptureFixture) -> None:
    registry = ToolRegistry()
    b_tool = Tool("b_tool", "B", obj({}), _noop, category="zwei")
    a_tool = Tool("a_tool", "A", obj({}), _noop, category="zwei")
    z_tool = Tool("z_tool", "Z", obj({}), _noop, category="eins")
    for item in (b_tool, a_tool, z_tool):
        registry.register(item)
    assert len(registry) == 3
    assert registry.get("a_tool") is a_tool
    assert registry.get("fehlt") is None
    assert registry.names() == ["a_tool", "b_tool", "z_tool"]
    # Sortiert nach Kategorie, dann Name.
    assert [t.name for t in registry.all()] == ["z_tool", "a_tool", "b_tool"]

    with caplog.at_level(logging.WARNING, logger="jarvis.plugins.base"):
        registry.register(Tool("a_tool", "A", obj({}), _noop))  # gleicher Handler: still
        assert not caplog.records

        async def other(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
            return ToolResult("anders")

        replacement = Tool("a_tool", "A2", obj({}), other)
        registry.register(replacement)
    assert "überschrieben" in caplog.text
    assert registry.get("a_tool") is replacement
    assert len(registry) == 3


def test_obj_and_spec() -> None:
    assert obj({"x": {"type": "string"}}) == {
        "type": "object",
        "properties": {"x": {"type": "string"}},
    }
    schema = obj({"x": {"type": "string"}}, ["x"])
    assert schema["required"] == ["x"]
    spec = Tool("demo", "Beschreibung", schema, _noop).spec()
    assert (spec.name, spec.description, spec.parameters) == ("demo", "Beschreibung", schema)


def test_resolve_level_static_and_dynamic(h: Harness) -> None:
    static = Tool("s", "", obj({}), _noop, level=Level.CONFIRM)
    dynamic = Tool(
        "d",
        "",
        obj({}),
        _noop,
        level=lambda args, ctx: Level.FORBIDDEN if args.get("gefährlich") else Level.AUTO,
    )
    assert static.resolve_level({}, h.ctx) is Level.CONFIRM
    assert dynamic.resolve_level({}, h.ctx) is Level.AUTO
    assert dynamic.resolve_level({"gefährlich": True}, h.ctx) is Level.FORBIDDEN


def test_tool_decorator_and_load_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    # Echte Plugins zuerst importieren, damit ihre Werkzeuge in der Original-Liste landen;
    # die Test-Werkzeuge kommen nur in eine Kopie, die danach verworfen wird.
    load_plugins(ToolRegistry())
    monkeypatch.setattr(base, "_DECLARED", list(base._DECLARED))

    @tool(name="test_hallo", description="Grüßt.", category="test", timeout_s=3)
    async def hallo(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult("Hallo!")

    @tool(name="test_aus", description="Deaktiviert.")
    async def aus(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult("nie")

    registry = load_plugins(ToolRegistry(), disabled=["test_aus"])
    item = registry.get("test_hallo")
    assert item is not None
    assert item.handler is hallo
    assert item.parameters == {"type": "object", "properties": {}}
    assert (item.category, item.timeout_s, item.level) == ("test", 3, Level.AUTO)
    assert registry.get("test_aus") is None


def test_real_plugins_are_loaded() -> None:
    registry = load_plugins(ToolRegistry())
    for name in ("files_read", "files_delete", "reminder_create", "system_status"):
        assert registry.get(name) is not None


# ------------------------------------------------------------------ Parameterprüfung


def test_coerce_arguments_repairs_typical_model_mistakes() -> None:
    schema = obj(
        {
            "count": {"type": "integer"},
            "ratio": {"type": "number"},
            "flag": {"type": "boolean"},
            "off": {"type": "boolean"},
            "label": {"type": "string"},
            "whole": {"type": "integer"},
            "items": {"type": "array"},
        },
        ["count"],
    )
    args = coerce_arguments(
        schema,
        {
            "count": " -5 ",
            "ratio": "1,5",
            "flag": "ja",
            "off": "nein",
            "label": 42,
            "whole": 3.0,
            "items": ["a"],
            "extra": "bleibt",
        },
    )
    assert args == {
        "count": -5,
        "ratio": 1.5,
        "flag": True,
        "off": False,
        "label": "42",
        "whole": 3,
        "items": ["a"],
        "extra": "bleibt",
    }


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({}, "Pflichtfeld 'name' fehlt"),
        ({"name": ""}, "Pflichtfeld 'name' fehlt"),
        ({"name": None}, "Pflichtfeld 'name' fehlt"),
        ({"name": "x", "count": "viele"}, "'count' hat den falschen Typ"),
        ({"name": "x", "count": True}, "'count' muss eine ganze Zahl sein"),
        ({"name": "x", "ratio": "abc"}, "'ratio' muss eine Zahl sein"),
        ({"name": "x", "mode": "turbo"}, "'mode' muss einer von"),
        ({"name": ["x"]}, "'name' hat den falschen Typ"),
    ],
)
def test_coerce_arguments_rejects_invalid_input(arguments: dict[str, Any], message: str) -> None:
    schema = obj(
        {
            "name": {"type": "string"},
            "count": {"type": "integer"},
            "ratio": {"type": "number"},
            "mode": {"type": "string", "enum": ["leise", "laut"]},
        },
        ["name"],
    )
    with pytest.raises(ValueError, match=message):
        coerce_arguments(schema, arguments)


def test_coerce_arguments_ignores_none_for_optional_fields() -> None:
    schema = obj({"name": {"type": "string"}, "count": {"type": "integer"}}, ["name"])
    assert coerce_arguments(schema, {"name": "x", "count": None}) == {"name": "x", "count": None}


# ------------------------------------------------------------------ Verfügbare Werkzeuge


def test_available_tools_filters_forbidden_and_unavailable(db: Database) -> None:
    h = make_harness(db, tool_levels={"gesperrt": "forbidden", "lockerer": "auto"})
    h.add("normal")
    h.add("gesperrt")
    h.add("lockerer", level=Level.CONFIRM)
    h.add("offline", available=lambda ctx: "kein Netz")
    h.add("online", available=lambda ctx: None)

    names = [t.name for t in h.executor.available_tools(h.ctx)]
    assert names == ["lockerer", "normal", "online"]
    assert [t.name for t in h.executor.available_tools(h.ctx, names=["normal", "offline"])] == [
        "normal"
    ]
    assert [s.name for s in h.executor.specs(h.ctx, names=["online"])] == ["online"]


def test_level_overrides_from_config(db: Database) -> None:
    h = make_harness(
        db, tool_levels={"lockern": "auto", "verschaerfen": "confirm", "erlauben": "auto"}
    )
    loosen = h.add("lockern", level=Level.CONFIRM)
    tighten = h.add("verschaerfen", level=Level.AUTO)
    never = h.add("erlauben", level=Level.FORBIDDEN)
    assert h.executor.level_for(loosen, {}, h.ctx) is Level.AUTO
    assert h.executor.level_for(tighten, {}, h.ctx) is Level.CONFIRM
    # Was das Werkzeug selbst verbietet, kann keine Konfiguration erlauben.
    assert h.executor.level_for(never, {}, h.ctx) is Level.FORBIDDEN


# ------------------------------------------------------------------ Ausführung


async def test_auto_tool_runs_directly_and_is_audited(h: Harness) -> None:
    h.add("lesen", parameters=obj({"count": {"type": "integer"}}, ["count"]))
    with h.events.subscribe(prefixes=["tool."]) as sub:
        result = await h.executor.execute("lesen", {"count": "3"}, h.ctx)
        started = await sub.get(limit_s=1)
        finished = await sub.get(limit_s=1)

    assert result.ok
    assert result.text == "lesen ok"
    assert h.calls == [("lesen", {"count": 3})]  # Text "3" wurde zur Zahl
    assert h.broker.pending() == []

    assert started is not None
    assert started.type == "tool.started"
    assert started.data == {"name": "lesen", "arguments": {"count": "3"}, "source": "test"}
    assert started.session_id == "sitzung-1"
    assert finished is not None
    assert finished.type == "tool.finished"
    assert finished.data["ok"] is True
    assert finished.data["decision"] == "auto"
    assert finished.data["preview"] == "lesen ok"

    [row] = h.audit_rows()
    assert row["tool"] == "lesen"
    assert row["level"] == "auto"
    assert row["decision"] == "auto"
    assert row["ok"] == 1
    assert row["source"] == "test"
    assert row["session_id"] == "sitzung-1"
    assert row["arguments"] == '{"count": 3}'
    assert row["result"] == "lesen ok"
    assert row["duration_ms"] >= 0


async def test_unknown_tool(h: Harness) -> None:
    result = await h.executor.execute("gibt_es_nicht", {}, h.ctx)
    assert not result.ok
    assert "Unbekanntes Werkzeug 'gibt_es_nicht'" in result.text


async def test_invalid_arguments_are_rejected_before_running(h: Harness) -> None:
    h.add("schreiben", parameters=obj({"path": {"type": "string"}}, ["path"]))
    result = await h.executor.execute("schreiben", {}, h.ctx)
    assert not result.ok
    assert result.text == "Ungültige Parameter für schreiben: Pflichtfeld 'path' fehlt"
    assert h.calls == []


async def test_unavailable_tool_is_refused(h: Harness) -> None:
    h.add("mail", available=lambda ctx: "kein Google-Konto verbunden")
    result = await h.executor.execute("mail", {}, h.ctx)
    assert not result.ok
    assert result.text == "mail ist nicht verfügbar: kein Google-Konto verbunden"
    assert h.calls == []


async def test_level_function_errors_become_tool_errors(h: Harness) -> None:
    def broken_level(args: dict[str, Any], ctx: ToolContext) -> Level:
        raise ValueError("ungültiger Pfad")

    h.add("kaputt", level=broken_level)
    result = await h.executor.execute("kaputt", {}, h.ctx)
    assert not result.ok
    assert result.text == "kaputt: ungültiger Pfad"
    assert h.calls == []


async def test_confirm_tool_runs_after_approval(h: Harness) -> None:
    h.add(
        "loeschen",
        level=Level.CONFIRM,
        summary=lambda args: f"Wirklich löschen: {args.get('path')}?",
    )
    task = asyncio.create_task(h.executor.execute("loeschen", {"path": "a.txt"}, h.ctx))
    pending = await _pending(h.broker)
    assert h.calls == []  # wartet auf die Antwort
    assert pending["tool"] == "loeschen"
    assert pending["summary"] == "Wirklich löschen: a.txt?"
    assert pending["details"] == {"path": "a.txt"}
    assert pending["session_id"] == "sitzung-1"
    assert pending["source"] == "test"

    h.broker.resolve(pending["id"], True)
    result = await asyncio.wait_for(task, 2)
    assert result.ok
    assert h.calls == [("loeschen", {"path": "a.txt"})]
    [row] = h.audit_rows()
    assert (row["level"], row["decision"], row["ok"]) == ("confirm", "approved", 1)


async def test_confirm_tool_default_summary(h: Harness) -> None:
    h.add("senden", level=Level.CONFIRM)
    task = asyncio.create_task(h.executor.execute("senden", {"path": "b.txt"}, h.ctx))
    pending = await _pending(h.broker)
    assert pending["summary"] == "senden: path=b.txt"
    h.broker.resolve(pending["id"], False)
    await task


async def test_confirm_tool_denied(h: Harness) -> None:
    h.add("loeschen", level=Level.CONFIRM)
    task = asyncio.create_task(h.executor.execute("loeschen", {"path": "a.txt"}, h.ctx))
    pending = await _pending(h.broker)
    h.broker.resolve(pending["id"], False)
    result = await asyncio.wait_for(task, 2)
    assert not result.ok
    assert result.text == "Der Benutzer hat die Aktion abgelehnt."
    assert h.calls == []
    [row] = h.audit_rows()
    assert (row["level"], row["decision"], row["ok"]) == ("confirm", "denied", 0)


async def test_confirm_tool_without_answer(db: Database) -> None:
    h = make_harness(db, confirm_timeout_s=0.05)
    h.add("loeschen", level=Level.CONFIRM)
    result = await asyncio.wait_for(h.executor.execute("loeschen", {}, h.ctx), 2)
    assert not result.ok
    assert result.text == "Keine Bestätigung erhalten – Aktion nicht ausgeführt."
    assert h.calls == []
    assert h.audit_rows()[0]["decision"] == "timeout"


async def test_forbidden_tool_is_refused(h: Harness) -> None:
    h.add("formatieren", level=Level.FORBIDDEN)
    h.broker.auto_answer = True  # selbst eine Zustimmung hilft nicht
    result = await h.executor.execute("formatieren", {"path": "C:/"}, h.ctx)
    assert not result.ok
    assert result.text == "Aus Sicherheitsgründen nicht erlaubt: formatieren (path=C:/)"
    assert h.calls == []
    [row] = h.audit_rows()
    assert (row["level"], row["decision"], row["ok"]) == ("forbidden", "forbidden", 0)


async def test_forbidden_by_config_is_refused(db: Database) -> None:
    h = make_harness(db, tool_levels={"shell": "forbidden"})
    h.add("shell")
    result = await h.executor.execute("shell", {}, h.ctx)
    assert not result.ok
    assert "nicht erlaubt" in result.text
    assert h.calls == []


async def test_dynamic_level_depends_on_arguments(h: Harness) -> None:
    h.add(
        "lesen",
        level=lambda args, ctx: Level.FORBIDDEN if args["path"].endswith(".env") else Level.AUTO,
    )
    assert (await h.executor.execute("lesen", {"path": "notiz.txt"}, h.ctx)).ok
    refused = await h.executor.execute("lesen", {"path": "geheim/.env"}, h.ctx)
    assert not refused.ok
    assert h.calls == [("lesen", {"path": "notiz.txt"})]


async def test_killswitch_blocks_all_tools(h: Harness) -> None:
    h.add("harmlos")
    h.killswitch.engage("Test")
    result = await h.executor.execute("harmlos", {"path": "x"}, h.ctx)
    assert not result.ok
    assert "Not-Aus ist aktiv" in result.text
    assert h.calls == []
    [row] = h.audit_rows()
    assert (row["level"], row["decision"]) == ("-", "killswitch")

    h.killswitch.release()
    assert (await h.executor.execute("harmlos", {}, h.ctx)).ok
    assert h.calls == [("harmlos", {})]


async def test_killswitch_during_confirmation_blocks_execution(h: Harness) -> None:
    h.add("loeschen", level=Level.CONFIRM)
    task = asyncio.create_task(h.executor.execute("loeschen", {}, h.ctx))
    pending = await _pending(h.broker)
    h.killswitch.engage("während der Rückfrage")
    h.broker.resolve(pending["id"], True)
    result = await asyncio.wait_for(task, 2)
    assert not result.ok
    assert result.text == "Not-Aus wurde ausgelöst."
    assert h.calls == []
    assert h.audit_rows()[0]["decision"] == "killswitch"


async def test_sensitive_results_are_flagged(h: Harness) -> None:
    h.add("geheim_lesen", result=ToolResult("Kontostand 1234 €", sensitive=True))
    h.add("oeffentlich")
    assert (await h.executor.execute("geheim_lesen", {}, h.ctx)).sensitive is True
    assert (await h.executor.execute("oeffentlich", {}, h.ctx)).sensitive is False


async def test_secrets_in_arguments_are_redacted_in_audit_log(h: Harness) -> None:
    key = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
    h.add("abrufen", parameters=obj({"token": {"type": "string"}}))
    await h.executor.execute("abrufen", {"token": key}, h.ctx)
    [row] = h.audit_rows()
    assert key not in row["arguments"]
    assert REDACTED in row["arguments"]


async def test_handler_errors_become_error_results(h: Harness) -> None:
    async def broken(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise RuntimeError("Festplatte voll")

    h.add("kaputt", handler=broken)
    result = await h.executor.execute("kaputt", {}, h.ctx)
    assert not result.ok
    assert result.text == "kaputt fehlgeschlagen: RuntimeError: Festplatte voll"
    assert h.audit_rows()[0]["ok"] == 0


async def test_slow_tools_time_out(h: Harness) -> None:
    async def slow(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(5)
        return ToolResult("zu spät")

    h.add("langsam", handler=slow, timeout_s=0.05)
    result = await asyncio.wait_for(h.executor.execute("langsam", {}, h.ctx), 2)
    assert not result.ok
    assert "hat zu lange gebraucht" in result.text


async def test_long_results_are_truncated(db: Database) -> None:
    h = make_harness(db, max_chars=500)
    h.add("viel", result=ToolResult("x" * 2000))
    result = await h.executor.execute("viel", {}, h.ctx)
    assert result.ok
    assert result.text.startswith("x" * 500 + "\n… (gekürzt, 2000 Zeichen gesamt)")
    assert len(result.text) < 600


async def test_images_are_announced(h: Harness) -> None:
    image = ImageRef(url="/api/images/file/a.png", caption="Katze", path="/tmp/a.png")
    h.add("bild", result=ToolResult("Bild erstellt", images=[image]))
    with h.events.subscribe(types=["image.new"]) as sub:
        result = await h.executor.execute("bild", {}, h.ctx)
        event = await sub.get(limit_s=1)
    assert result.images == [image]
    assert event is not None
    assert event.data == {"url": "/api/images/file/a.png", "caption": "Katze", "path": "/tmp/a.png"}
    assert event.session_id == "sitzung-1"
    finished = h.events.recent(prefix="tool.finished")[-1]
    assert finished.data["images"] == [event.data]


async def test_cancelled_tool_is_audited_and_reraised(h: Harness) -> None:
    started = asyncio.Event()

    async def forever(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        started.set()
        await asyncio.sleep(10)
        return ToolResult("nie")

    h.add("endlos", handler=forever)
    task = asyncio.create_task(h.executor.execute("endlos", {}, h.ctx))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    [row] = h.audit_rows()
    assert (row["decision"], row["ok"]) == ("cancelled", 0)
