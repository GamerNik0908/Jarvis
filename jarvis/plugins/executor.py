"""Führt Werkzeug-Aufrufe aus – immer über Not-Aus, Berechtigungen und Protokoll.

Egal ob der Aufruf von Gemini, Ollama, Claude Code (über die MCP-Brücke) oder
aus einem Hintergrundprojekt kommt: Jeder Aufruf läuft durch
:meth:`ToolExecutor.execute`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable, Mapping
from typing import Any

from jarvis.core.config import AgentConfig, SecurityConfig
from jarvis.core.events import EventBus
from jarvis.plugins.base import Tool, ToolContext, ToolRegistry, ToolResult
from jarvis.router.base import ToolSpec
from jarvis.security.audit import AuditEntry, AuditLog
from jarvis.security.killswitch import KillSwitch
from jarvis.security.permissions import ConfirmationBroker, Level, describe_arguments

log = logging.getLogger(__name__)

_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


def coerce_arguments(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Prüft Pflichtfelder und einfache Typen; korrigiert typische Modellfehler.

    Kleine lokale Modelle schicken Zahlen gern als Text ("5") oder Wahrheitswerte
    als "true" – das wird hier repariert statt abgelehnt.
    """
    args = dict(arguments)
    properties: Mapping[str, Any] = schema.get("properties", {}) or {}
    for name in schema.get("required", []) or []:
        if name not in args or args[name] in (None, ""):
            raise ValueError(f"Pflichtfeld '{name}' fehlt")
    for name, value in list(args.items()):
        spec = properties.get(name)
        if not isinstance(spec, Mapping) or value is None:
            continue
        expected = spec.get("type")
        if expected == "integer" and isinstance(value, str) and value.strip().lstrip("-").isdigit():
            args[name] = int(value)
        elif expected == "number" and isinstance(value, str):
            try:
                args[name] = float(value.replace(",", "."))
            except ValueError as exc:
                raise ValueError(f"'{name}' muss eine Zahl sein") from exc
        elif expected == "boolean" and isinstance(value, str):
            args[name] = value.strip().lower() in {"true", "1", "ja", "yes"}
        elif expected == "integer" and isinstance(value, float) and value.is_integer():
            args[name] = int(value)
        elif expected == "string" and isinstance(value, int | float):
            args[name] = str(value)
        types = _JSON_TYPES.get(str(expected))
        if types and not isinstance(args[name], types):
            raise ValueError(f"'{name}' hat den falschen Typ (erwartet {expected})")
        if expected == "integer" and isinstance(args[name], bool):
            raise ValueError(f"'{name}' muss eine ganze Zahl sein")
        enum = spec.get("enum")
        if enum and args[name] not in enum:
            raise ValueError(f"'{name}' muss einer von {enum} sein")
    return args


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        broker: ConfirmationBroker,
        audit: AuditLog,
        killswitch: KillSwitch,
        events: EventBus,
        security: SecurityConfig,
        agent: AgentConfig,
    ) -> None:
        self.registry = registry
        self._broker = broker
        self._audit = audit
        self._killswitch = killswitch
        self._events = events
        self._security = security
        self._agent = agent

    def available_tools(
        self, ctx: ToolContext, *, names: Iterable[str] | None = None
    ) -> list[Tool]:
        wanted = set(names) if names is not None else None
        result = []
        for item in self.registry.all():
            if wanted is not None and item.name not in wanted:
                continue
            if self._security.tool_levels.get(item.name) == Level.FORBIDDEN.value:
                continue
            reason = item.available(ctx) if item.available else None
            if reason:
                continue
            result.append(item)
        return result

    def specs(self, ctx: ToolContext, *, names: Iterable[str] | None = None) -> list[ToolSpec]:
        return [t.spec() for t in self.available_tools(ctx, names=names)]

    def level_for(self, item: Tool, args: dict[str, Any], ctx: ToolContext) -> Level:
        level = item.resolve_level(args, ctx)
        override = self._security.tool_levels.get(item.name)
        if override is not None and level != Level.FORBIDDEN:
            # Eine Konfiguration darf lockern oder verschärfen – aber nie etwas
            # erlauben, das das Werkzeug selbst für verboten hält.
            level = Level(override)
        return level

    async def execute(
        self, name: str, arguments: Mapping[str, Any], ctx: ToolContext
    ) -> ToolResult:
        started = time.monotonic()
        raw_args = dict(arguments or {})
        if self._killswitch.engaged:
            return self._finish(
                name,
                raw_args,
                ctx,
                "-",
                "killswitch",
                ToolResult.error(
                    "Not-Aus ist aktiv – keine Aktionen möglich, bis er freigegeben wird."
                ),
                started,
            )
        item = self.registry.get(name)
        if item is None:
            return ToolResult.error(f"Unbekanntes Werkzeug '{name}'")
        try:
            args = coerce_arguments(item.parameters, raw_args)
        except ValueError as exc:
            return ToolResult.error(f"Ungültige Parameter für {name}: {exc}")
        reason = item.available(ctx) if item.available else None
        if reason:
            return ToolResult.error(f"{name} ist nicht verfügbar: {reason}")

        try:
            level = self.level_for(item, args, ctx)
        except Exception as exc:  # z. B. ungültiger Pfad
            return ToolResult.error(f"{name}: {exc}")

        if level == Level.FORBIDDEN:
            return self._finish(
                name,
                args,
                ctx,
                level,
                "forbidden",
                ToolResult.error(
                    f"Aus Sicherheitsgründen nicht erlaubt: {name} ({describe_arguments(args)})"
                ),
                started,
            )

        decision = "auto"
        if level == Level.CONFIRM:
            summary = item.summary(args) if item.summary else f"{name}: {describe_arguments(args)}"
            approved, decision = await self._broker.request(
                tool=name,
                summary=summary,
                details=args,
                session_id=ctx.session_id,
                source=ctx.source,
            )
            if not approved:
                text = (
                    "Der Benutzer hat die Aktion abgelehnt."
                    if decision == "denied"
                    else "Keine Bestätigung erhalten – Aktion nicht ausgeführt."
                )
                return self._finish(
                    name, args, ctx, level, decision, ToolResult.error(text), started
                )
            if self._killswitch.engaged:
                return self._finish(
                    name,
                    args,
                    ctx,
                    level,
                    "killswitch",
                    ToolResult.error("Not-Aus wurde ausgelöst."),
                    started,
                )

        self._events.publish(
            "tool.started",
            {"name": name, "arguments": _preview_args(args), "source": ctx.source},
            session_id=ctx.session_id,
        )
        try:
            result = await asyncio.wait_for(item.handler(args, ctx), item.timeout_s)
        except TimeoutError:
            result = ToolResult.error(f"{name} hat zu lange gebraucht (>{item.timeout_s:.0f} s)")
        except asyncio.CancelledError:
            self._finish(
                name, args, ctx, level, "cancelled", ToolResult.error("abgebrochen"), started
            )
            raise
        except Exception as exc:
            log.exception("Werkzeug %s fehlgeschlagen", name)
            result = ToolResult.error(f"{name} fehlgeschlagen: {type(exc).__name__}: {exc}")
        limit = self._agent.tool_result_max_chars
        if len(result.text) > limit:
            result.text = result.text[:limit] + f"\n… (gekürzt, {len(result.text)} Zeichen gesamt)"
        return self._finish(name, args, ctx, level, decision, result, started)

    def _finish(
        self,
        name: str,
        args: dict[str, Any],
        ctx: ToolContext,
        level: Level | str,
        decision: str,
        result: ToolResult,
        started: float,
    ) -> ToolResult:
        duration = int((time.monotonic() - started) * 1000)
        try:
            self._audit.record(
                AuditEntry(
                    source=ctx.source,
                    tool=name,
                    arguments=args,
                    level=str(level),
                    decision=decision,
                    ok=result.ok,
                    result=result.text,
                    duration_ms=duration,
                    session_id=ctx.session_id,
                )
            )
        except Exception:
            log.exception("Aktionsprotokoll konnte nicht geschrieben werden")
        self._events.publish(
            "tool.finished",
            {
                "name": name,
                "source": ctx.source,
                "ok": result.ok,
                "decision": decision,
                "preview": result.text[:300],
                "images": [img.__dict__ for img in result.images],
                "duration_ms": duration,
            },
            session_id=ctx.session_id,
        )
        for image in result.images:
            self._events.publish("image.new", dict(image.__dict__), session_id=ctx.session_id)
        return result


def _preview_args(args: Mapping[str, Any]) -> dict[str, str]:
    preview = {}
    for key, value in args.items():
        text = str(value)
        preview[key] = text if len(text) <= 160 else text[:157] + "…"
    return preview
