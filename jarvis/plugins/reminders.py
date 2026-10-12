"""Erinnerungen und Timer: JARVIS meldet sich zur angegebenen Zeit (Ton, Sprache, Benachrichtigung)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from jarvis.core.stores import REPEATS
from jarvis.core.timeutil import human, now, parse_iso
from jarvis.plugins.base import ToolContext, ToolResult, obj, tool


@tool(
    name="reminder_create",
    description=(
        "Create a reminder or timer. Give either 'due' as ISO local date-time (e.g. 2026-10-12T18:30) "
        "or 'in_minutes'. Optional repeat: daily, weekdays, weekly, monthly."
    ),
    parameters=obj(
        {
            "text": {"type": "string", "description": "What to remind about (German)"},
            "due": {"type": "string"},
            "in_minutes": {"type": "number"},
            "repeat": {"type": "string", "enum": list(REPEATS)},
        },
        ["text"],
    ),
    category="organisation",
)
async def reminder_create(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    if args.get("in_minutes") is not None:
        due = now() + timedelta(minutes=float(args["in_minutes"]))
    elif args.get("due"):
        try:
            due = parse_iso(str(args["due"]))
        except ValueError:
            return ToolResult.error(
                "Zeitpunkt nicht verstanden – bitte ISO-Format wie 2026-10-12T18:30"
            )
    else:
        return ToolResult.error("Bitte 'due' oder 'in_minutes' angeben.")
    if due < now() - timedelta(minutes=1):
        return ToolResult.error(f"Der Zeitpunkt {human(due)} liegt in der Vergangenheit.")
    reminder = ctx.app.reminders.create(str(args["text"]), due, args.get("repeat"))
    ctx.app.events.publish("reminders.changed", {})
    repeat = f", wiederholt: {reminder.repeat}" if reminder.repeat else ""
    return ToolResult(
        f"Erinnerung #{reminder.id} für {human(reminder.due)}{repeat}: {reminder.text}"
    )


@tool(
    name="reminder_list",
    description="List upcoming reminders and timers.",
    parameters=obj({}),
    category="organisation",
)
async def reminder_list(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    items = ctx.app.reminders.upcoming()
    if not items:
        return ToolResult("Keine offenen Erinnerungen.")
    return ToolResult(
        "\n".join(
            f"#{r.id} {human(r.due)}: {r.text}" + (f" ({r.repeat})" if r.repeat else "")
            for r in items
        )
    )


@tool(
    name="reminder_delete",
    description="Delete a reminder by id.",
    parameters=obj({"id": {"type": "integer"}}, ["id"]),
    category="organisation",
)
async def reminder_delete(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    if ctx.app.reminders.delete(int(args["id"])):
        ctx.app.events.publish("reminders.changed", {})
        return ToolResult("Erinnerung gelöscht.")
    return ToolResult.error("Erinnerung nicht gefunden.")
