"""Langzeit-Projekte als Werkzeuge: anlegen, Status abfragen, Rückfragen beantworten."""

from __future__ import annotations

from typing import Any

from jarvis.plugins.base import ToolContext, ToolResult, obj, tool


def _needs_projects(ctx: ToolContext) -> str | None:
    if ctx.app.projects is None:
        return "Projektmodus läuft nur im JARVIS-Hintergrunddienst"
    return None


@tool(
    name="project_create",
    description=(
        "Start a long-running project that JARVIS works on autonomously in the background (possibly "
        "over several days): JARVIS makes a plan with milestones and tasks, works through them, checks "
        "results and asks the user when stuck. Use for big goals, not for quick questions."
    ),
    parameters=obj(
        {
            "goal": {
                "type": "string",
                "description": "The project goal in the user's words, with all details",
            },
            "title": {"type": "string"},
            "priority": {"type": "integer", "description": "1 (highest) to 5, default 3"},
        },
        ["goal"],
    ),
    category="projekte",
    available=_needs_projects,
)
async def project_create(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    assert ctx.app.projects is not None
    project = await ctx.app.projects.create(
        str(args["goal"]),
        title=str(args.get("title") or "") or None,
        priority=int(args.get("priority") or 3),
    )
    return ToolResult(
        f"Projekt '{project['title']}' (#{project['id']}) angelegt. Ich erstelle jetzt den Plan und "
        "arbeite im Hintergrund daran – den Fortschritt siehst du im Dashboard unter 'Projekte'."
    )


@tool(
    name="project_list",
    description="List all projects with their status and progress.",
    parameters=obj({}),
    category="projekte",
    available=_needs_projects,
)
async def project_list(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    assert ctx.app.projects is not None
    projects = ctx.app.projects.list()
    if not projects:
        return ToolResult("Es gibt noch keine Projekte.")
    return ToolResult(
        "\n".join(
            f"#{p['id']} {p['title']} – {p['status']} ({p['done']}/{p['total']} Aufgaben, Priorität {p['priority']})"
            for p in projects
        )
    )


@tool(
    name="project_status",
    description="Detailed status of one project: plan, finished and open tasks, open questions.",
    parameters=obj({"id": {"type": "string"}}, ["id"]),
    category="projekte",
    available=_needs_projects,
)
async def project_status(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    assert ctx.app.projects is not None
    text = ctx.app.projects.status_text(str(args["id"]))
    return ToolResult(text) if text else ToolResult.error("Projekt nicht gefunden.")


@tool(
    name="project_control",
    description="Pause, resume or cancel a project.",
    parameters=obj(
        {
            "id": {"type": "string"},
            "action": {"type": "string", "enum": ["pause", "resume", "cancel"]},
        },
        ["id", "action"],
    ),
    category="projekte",
    available=_needs_projects,
)
async def project_control(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    assert ctx.app.projects is not None
    ok = ctx.app.projects.control(str(args["id"]), str(args["action"]))
    return ToolResult("Erledigt.") if ok else ToolResult.error("Projekt nicht gefunden.")


@tool(
    name="project_answer",
    description="Answer an open question that a project asked the user.",
    parameters=obj(
        {"question_id": {"type": "integer"}, "answer": {"type": "string"}},
        ["question_id", "answer"],
    ),
    category="projekte",
    available=_needs_projects,
)
async def project_answer(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    assert ctx.app.projects is not None
    if ctx.app.projects.answer(int(args["question_id"]), str(args["answer"])):
        return ToolResult("Danke – das Projekt arbeitet mit deiner Antwort weiter.")
    return ToolResult.error("Frage nicht gefunden oder schon beantwortet.")


def _in_project(ctx: ToolContext) -> str | None:
    if ctx.project_id is None or ctx.app.projects is None:
        return "nur innerhalb einer Projektaufgabe"
    return None


@tool(
    name="project_ask_user",
    description=(
        "Inside a project task: ask the user a question when you are truly stuck or an important "
        "decision is needed. Work continues on other tasks meanwhile."
    ),
    parameters=obj({"question": {"type": "string"}}, ["question"]),
    category="projekte",
    available=_in_project,
)
async def project_ask_user(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    assert ctx.app.projects is not None and ctx.project_id is not None
    question_id = ctx.app.projects.ask_user(ctx.project_id, str(args["question"]))
    return ToolResult(
        f"Frage #{question_id} an den Benutzer gestellt. Beende diese Aufgabe jetzt mit dem Hinweis, "
        "dass auf die Antwort gewartet wird."
    )
