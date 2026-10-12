"""Übersicht gestalten und Benachrichtigungen senden."""

from __future__ import annotations

from typing import Any

from jarvis.plugins.base import ToolContext, ToolResult, obj, tool


@tool(
    name="dashboard_card",
    description=(
        "Pin or update a card on the user's dashboard overview (e.g. a shopping list, a summary, "
        "weather, a status). Content supports Markdown. Same title = update the existing card."
    ),
    parameters=obj(
        {
            "title": {"type": "string"},
            "content": {"type": "string", "description": "Markdown"},
            "image_url": {"type": "string", "description": "Optional image URL from image tools"},
        },
        ["title", "content"],
    ),
    category="übersicht",
)
async def dashboard_card(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    card_id = ctx.app.cards.upsert(str(args["title"]), str(args["content"]), args.get("image_url"))
    ctx.app.events.publish("cards.changed", {"id": card_id})
    return ToolResult(f"Karte '{args['title']}' ist auf der Übersicht.")


@tool(
    name="dashboard_card_remove",
    description="Remove a card from the dashboard overview by its title.",
    parameters=obj({"title": {"type": "string"}}, ["title"]),
    category="übersicht",
)
async def dashboard_card_remove(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    title = str(args["title"]).lower()
    for card in ctx.app.cards.list():
        if card["title"].lower() == title:
            ctx.app.cards.delete(int(card["id"]))
            ctx.app.events.publish("cards.changed", {"id": card["id"]})
            return ToolResult("Karte entfernt.")
    return ToolResult.error("Keine Karte mit diesem Titel.")


@tool(
    name="notify_user",
    description="Show a desktop notification to the user (optionally also spoken aloud).",
    parameters=obj(
        {"title": {"type": "string"}, "message": {"type": "string"}, "speak": {"type": "boolean"}},
        ["message"],
    ),
    category="übersicht",
)
async def notify_user(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    ctx.app.notify(
        str(args.get("title") or "JARVIS"), str(args["message"]), speak=bool(args.get("speak"))
    )
    return ToolResult("Benachrichtigung gesendet.")
