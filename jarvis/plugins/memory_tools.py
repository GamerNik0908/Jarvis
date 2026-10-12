"""Langzeitgedächtnis als Werkzeug: merken, erinnern, vergessen."""

from __future__ import annotations

from typing import Any

from jarvis.plugins.base import ToolContext, ToolResult, obj, tool


def _memory_off(ctx: ToolContext) -> str | None:
    return None if ctx.app.config.memory.enabled else "Gedächtnis ist ausgeschaltet"


@tool(
    name="memory_remember",
    description=(
        "Store a lasting fact about the user or their preferences (e.g. 'Nik trinkt Kaffee schwarz', "
        "'Lieblingsband: …', 'Arbeitet bei …'). Use when the user shares something worth remembering."
    ),
    parameters=obj(
        {
            "fact": {"type": "string", "description": "One self-contained sentence in German"},
            "category": {
                "type": "string",
                "enum": ["fakt", "vorliebe", "person", "aufgabe", "notiz"],
            },
        },
        ["fact"],
    ),
    category="gedächtnis",
    available=_memory_off,
)
async def memory_remember(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    memory_id = await ctx.app.memory.remember(
        str(args["fact"]), category=str(args.get("category") or "fakt"), source=ctx.source
    )
    return ToolResult(f"Gemerkt (#{memory_id}).")


@tool(
    name="memory_recall",
    description="Search long-term memory for facts related to a topic.",
    parameters=obj({"query": {"type": "string"}}, ["query"]),
    category="gedächtnis",
    available=_memory_off,
)
async def memory_recall(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    memories = await ctx.app.memory.recall(str(args["query"]), limit=10)
    if not memories:
        return ToolResult("Dazu ist nichts gespeichert.")
    return ToolResult(
        "\n".join(f"#{m.id} [{m.category}] {m.text}" for m in memories), sensitive=True
    )


@tool(
    name="memory_forget",
    description="Delete a stored memory by its id (from memory_recall).",
    parameters=obj({"id": {"type": "integer"}}, ["id"]),
    category="gedächtnis",
    available=_memory_off,
)
async def memory_forget(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    if ctx.app.memory.forget(int(args["id"])):
        return ToolResult("Vergessen.")
    return ToolResult.error("Eintrag nicht gefunden.")
