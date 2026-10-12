"""Selbstverbesserung und Selbstauskunft: JARVIS erweitert sich mit Claude Code."""

from __future__ import annotations

from typing import Any

from jarvis.plugins.base import ToolContext, ToolResult, obj, tool


def _needs_improver(ctx: ToolContext) -> str | None:
    improver = ctx.app.improvements
    if improver is None:
        return "Selbstverbesserung läuft nur im JARVIS-Hintergrunddienst"
    return improver.unavailable_reason()


@tool(
    name="jarvis_improve",
    description=(
        "Extend or fix JARVIS itself (new feature, new tool/plugin, bug fix, UI change), e.g. "
        "'add a print option', 'add a tool to control the volume'. JARVIS programs it with Claude Code "
        "in a separate branch, runs the tests and shows the result in the dashboard for approval."
    ),
    parameters=obj(
        {
            "request": {
                "type": "string",
                "description": "What should be built or changed, as detailed as possible",
            }
        },
        ["request"],
    ),
    category="selbst",
    available=_needs_improver,
)
async def jarvis_improve(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    assert ctx.app.improvements is not None
    job = ctx.app.improvements.request(str(args["request"]), source=ctx.source)
    return ToolResult(
        f"Verbesserung #{job['id']} ist in Arbeit. Ich programmiere das mit Claude Code in einem "
        "eigenen Branch, teste es und melde mich zur Freigabe. Zwischenstand: Dashboard → "
        "'Selbstverbesserung'."
    )


@tool(
    name="jarvis_improvements",
    description="Status of JARVIS' self-improvement jobs (running, waiting for approval, merged).",
    parameters=obj({}),
    category="selbst",
    available=_needs_improver,
)
async def jarvis_improvements(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    assert ctx.app.improvements is not None
    jobs = ctx.app.improvements.list_jobs(limit=10)
    if not jobs:
        return ToolResult("Noch keine Selbstverbesserungen.")
    return ToolResult("\n".join(f"#{j['id']} [{j['status']}] {j['request'][:100]}" for j in jobs))


@tool(
    name="jarvis_capabilities",
    description="List everything JARVIS can currently do (all available tools and AI models).",
    parameters=obj({}),
    category="selbst",
)
async def jarvis_capabilities(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    tools = ctx.app.executor.available_tools(ctx)
    by_category: dict[str, list[str]] = {}
    for item in tools:
        by_category.setdefault(item.category, []).append(item.name)
    lines = [f"{cat}: {', '.join(names)}" for cat, names in sorted(by_category.items())]
    models = [
        m.label
        for m in ctx.app.router.models()
        if ctx.app.router.model_unavailable_reason(m) is None
    ]
    lines.append("Modelle bereit: " + (", ".join(models) or "keine"))
    return ToolResult("\n".join(lines))
