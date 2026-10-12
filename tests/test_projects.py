"""Langzeit-Projektmodus: Planen, Ausführen, Prüfen, Rückfragen, Steuerung und Tagesbericht."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from jarvis.core.app import JarvisApp
from jarvis.core.timeutil import now
from jarvis.projects.manager import (
    ACTIVE,
    CANCELLED,
    DONE,
    PAUSED,
    PLANNER_PROMPT,
    PLANNING,
    VERIFIER_PROMPT,
    WAITING,
    ProjectManager,
)
from jarvis.router.base import ChatResponse, ToolCall, UsageSegment
from jarvis.router.errors import ProviderRequestError

from .conftest import FakeProvider

PLAN: dict[str, Any] = {
    "title": "Website bauen",
    "summary": "Erst recherchieren, dann bauen.",
    "milestones": [
        {
            "title": "Start",
            "tasks": [
                {"title": "Recherche", "description": "Ideen sammeln", "kind": "research"},
                # 2 ist keine frühere Aufgabe, "x" keine Zahl – beides wird verworfen.
                {"title": "Umsetzen", "description": "Seite bauen", "depends_on": [1, 2, "x"]},
            ],
        }
    ],
}


@pytest.fixture
async def app(config_file: Path) -> AsyncIterator[JarvisApp]:
    application = JarvisApp.bootstrap(config_file, role="test", detect=False)
    application.config.projects.max_task_attempts = 2
    yield application
    await application.aclose()


@pytest.fixture
def fake(app: JarvisApp) -> FakeProvider:
    provider = app.router.providers["fake"]
    assert isinstance(provider, FakeProvider)
    return provider


@pytest.fixture
def projects(app: JarvisApp) -> ProjectManager:
    manager = ProjectManager(app)
    app.projects = manager  # macht project_ask_user & Co. verfügbar
    return manager


def reply(
    text: str = "", *calls: ToolCall, model: str = "fake-big", tokens: int = 100
) -> ChatResponse:
    return ChatResponse(
        text=text,
        provider="fake",
        model=model,
        requested_model=model,
        stop_reason="tool_use" if calls else "end_turn",
        usage=[UsageSegment(model=model, input_tokens=tokens, output_tokens=tokens // 5)],
        tool_calls=list(calls),
    )


def verdict(ok: bool, feedback: str = "") -> ChatResponse:
    return reply(json.dumps({"ok": ok, "feedback": feedback}))


def queue(fake: FakeProvider, *responses: ChatResponse, model: str = "fake-big") -> None:
    fake.scripted.setdefault(model, []).extend(responses)


async def planned_project(
    projects: ProjectManager, fake: FakeProvider, **kwargs: Any
) -> dict[str, Any]:
    """Legt ein Projekt an und lässt es planen (Plan mit Text drumherum, wie Modelle es tun)."""
    project = await projects.create("Baue mir eine Website\nmit vielen Details", **kwargs)
    queue(fake, reply(f"Hier ist der Plan:\n```json\n{json.dumps(PLAN)}\n```\nViel Erfolg!"))
    assert await projects.step() is True
    result = projects.get(project["id"])
    assert result is not None
    return result


def task_by_position(projects: ProjectManager, project_id: str, position: int) -> dict[str, Any]:
    project = projects.get(project_id)
    assert project is not None
    return next(t for t in project["tasks"] if t["position"] == position)


async def test_create_starts_in_planning(projects: ProjectManager) -> None:
    project = await projects.create("Umzug organisieren\nmit Kisten", priority=9)
    assert project["status"] == PLANNING
    assert project["title"] == "Umzug organisieren"
    assert project["priority"] == 5  # auf 1–5 begrenzt
    assert project["tasks"] == [] and project["plan"] is None
    assert [e["kind"] for e in project["events"]] == ["created"]

    listed = projects.list()
    assert len(listed) == 1
    assert listed[0]["id"] == project["id"]
    assert (listed[0]["done"], listed[0]["total"], listed[0]["open_questions"]) == (0, 0, 0)


async def test_planning_creates_tasks_with_dependencies(
    app: JarvisApp, projects: ProjectManager, fake: FakeProvider
) -> None:
    project = await planned_project(projects, fake)

    assert project["status"] == ACTIVE
    assert project["title"] == "Website bauen"
    assert project["summary"] == "Erst recherchieren, dann bauen."
    assert project["plan"] == PLAN
    tasks = project["tasks"]
    assert [(t["position"], t["title"], t["kind"]) for t in tasks] == [
        (1, "Recherche", "research"),
        (2, "Umsetzen", "chat"),
    ]
    assert [json.loads(t["depends_on"]) for t in tasks] == [[], [1]]
    assert all(t["status"] == "pending" and t["milestone"] == "Start" for t in tasks)
    assert project["events"][0]["kind"] == "planned"

    request = fake.requests[0]
    assert request.system == PLANNER_PROMPT
    assert "Baue mir eine Website" in request.messages[0].content
    assert app.costs.spent_for_project(project["id"]) > 0  # Kosten dem Projekt zugeordnet


async def test_planning_failure_asks_user_and_answer_replans(
    projects: ProjectManager, fake: FakeProvider
) -> None:
    project = await projects.create("Etwas Unklares")
    queue(fake, reply("Das verstehe ich nicht."))
    assert await projects.step() is True

    current = projects.get(project["id"])
    assert current is not None and current["status"] == WAITING
    question = current["questions"][0]
    assert question["status"] == "open"
    assert "keinen Plan erstellen" in question["question"]
    assert projects.list()[0]["open_questions"] == 1

    assert projects.answer(question["id"], "Eine Website mit drei Seiten") is True
    assert projects.answer(question["id"], "nochmal") is False  # schon beantwortet
    current = projects.get(project["id"])
    assert current is not None and current["status"] == PLANNING  # noch kein Plan

    queue(fake, reply(json.dumps(PLAN)))
    assert await projects.step() is True
    current = projects.get(project["id"])
    assert current is not None and current["status"] == ACTIVE and current["total"] == 2


async def test_task_is_executed_verified_and_done(
    app: JarvisApp, projects: ProjectManager, fake: FakeProvider
) -> None:
    project = await planned_project(projects, fake)
    queue(fake, reply("Ich habe drei Ideen gesammelt."), verdict(True))
    assert await projects.step() is True

    task = task_by_position(projects, project["id"], 1)
    assert task["status"] == "done"
    assert task["result"] == "Ich habe drei Ideen gesammelt."
    assert task["attempts"] == 1
    assert task["feedback"] is None

    agent_request, verify_request = fake.requests[1], fake.requests[2]
    context = agent_request.messages[-1].content
    assert "AKTUELLE AUFGABE 1: Recherche" in context
    assert "Projektziel: Baue mir eine Website" in context
    assert (app.workspace / "projekte" / project["id"]).is_dir()
    assert any(spec.name == "project_ask_user" for spec in agent_request.tools)
    assert verify_request.system == VERIFIER_PROMPT
    assert "Ich habe drei Ideen gesammelt." in verify_request.messages[0].content
    # Projektaufgaben landen nicht im Chatverlauf.
    assert app.conversations.messages(f"projekt-{project['id']}") == []
    assert projects._tasks_today() == 2  # Planung + Aufgabe

    # Aufgabe 2 hängt von 1 ab und ist jetzt dran; das Ergebnis von 1 steht im Kontext.
    queue(fake, reply("Seite gebaut."), verdict(True))
    assert await projects.step() is True
    assert "Bereits erledigt:" in fake.requests[3].messages[-1].content
    assert task_by_position(projects, project["id"], 2)["status"] == "done"

    # Alles erledigt → Projekt wird abgeschlossen.
    assert await projects.step() is False
    await asyncio.sleep(0)
    finished = projects.get(project["id"])
    assert finished is not None and finished["status"] == DONE
    assert (finished["done"], finished["total"]) == (2, 2)
    assert any(e.data.get("title") == "Projekt abgeschlossen" for e in app.events.recent(50))


async def test_dependencies_block_later_tasks(projects: ProjectManager, fake: FakeProvider) -> None:
    project = await planned_project(projects, fake)
    first = task_by_position(projects, project["id"], 1)
    projects.db.execute("UPDATE project_tasks SET status = 'blocked' WHERE id = ?", (first["id"],))
    # Aufgabe 2 wartet auf Aufgabe 1 → nichts zu tun.
    assert await projects.step() is False


async def test_failed_verification_retries_then_blocks_and_asks(
    projects: ProjectManager, fake: FakeProvider
) -> None:
    project = await planned_project(projects, fake)
    queue(fake, reply("Halb fertig."), verdict(False, "Quellen fehlen"))
    assert await projects.step() is True

    task = task_by_position(projects, project["id"], 1)
    assert (task["status"], task["attempts"], task["feedback"]) == ("pending", 1, "Quellen fehlen")
    assert task["result"] == "Halb fertig."

    queue(fake, reply("Immer noch halb."), verdict(False, "Quellen fehlen weiterhin"))
    assert await projects.step() is True
    assert (
        "Hinweis aus dem letzten Versuch: Quellen fehlen" in fake.requests[-2].messages[-1].content
    )

    task = task_by_position(projects, project["id"], 1)
    assert (task["status"], task["attempts"]) == ("blocked", 2)
    current = projects.get(project["id"])
    assert current is not None and current["status"] == ACTIVE
    question = current["questions"][0]
    assert question["task_id"] == task["id"]
    assert "klappt nach 2 Versuchen nicht" in question["question"]
    assert "Offene Frage" in (projects.status_text(project["id"]) or "")

    # Eine Antwort gibt der Aufgabe eine neue Chance – mit dem Hinweis als Feedback.
    assert projects.answer(question["id"], "Nimm Wikipedia als Quelle") is True
    task = task_by_position(projects, project["id"], 1)
    assert (task["status"], task["attempts"]) == ("pending", 0)
    assert "Nimm Wikipedia als Quelle" in task["feedback"]


async def test_skip_answer_skips_blocked_task(projects: ProjectManager, fake: FakeProvider) -> None:
    project = await planned_project(projects, fake)
    first = task_by_position(projects, project["id"], 1)
    projects._retry_or_block(first, 2, "geht nicht")
    question = (projects.get(project["id"]) or {})["questions"][0]

    assert projects.answer(question["id"], "Überspringen") is True
    assert task_by_position(projects, project["id"], 1)["status"] == "skipped"
    # Übersprungene Aufgaben zählen als erledigte Abhängigkeit.
    queue(fake, reply("Seite gebaut."), verdict(True))
    assert await projects.step() is True
    assert task_by_position(projects, project["id"], 2)["status"] == "done"


async def test_agent_error_counts_as_failed_attempt(
    projects: ProjectManager, fake: FakeProvider
) -> None:
    project = await planned_project(projects, fake)
    fake.behaviours["fake-big"] = ProviderRequestError("kaputt", provider="fake", model="fake-big")
    assert await projects.step() is True

    task = task_by_position(projects, project["id"], 1)
    assert (task["status"], task["attempts"]) == ("pending", 1)
    assert task["feedback"].startswith("Fehler bei der Ausführung")
    assert "kaputt" in task["feedback"]


async def test_project_question_from_task_waits_for_user(
    projects: ProjectManager, fake: FakeProvider
) -> None:
    project = await planned_project(projects, fake)
    queue(
        fake,
        reply("", ToolCall("q", "project_ask_user", {"question": "Welche Farbe?"})),
        reply("Ich warte auf die Antwort."),
    )
    assert await projects.step() is True
    assert len(fake.requests) == 3  # Plan + zwei Agent-Schritte, keine Prüfung

    task = task_by_position(projects, project["id"], 1)
    assert task["status"] == WAITING
    assert task["result"] == "Ich warte auf die Antwort."
    question = (projects.get(project["id"]) or {})["questions"][0]
    assert (question["question"], question["task_id"]) == ("Welche Farbe?", task["id"])
    assert "? 1. Recherche" in (projects.status_text(project["id"]) or "")
    # Aufgabe 2 hängt von der wartenden Aufgabe ab.
    assert await projects.step() is False

    assert projects.answer(question["id"], "Blau") is True
    task = task_by_position(projects, project["id"], 1)
    assert task["status"] == "pending"
    queue(fake, reply("Alles in Blau."), verdict(True))
    assert await projects.step() is True
    context = fake.requests[3].messages[-1].content
    assert "Welche Farbe? → Blau" in context
    assert task_by_position(projects, project["id"], 1)["status"] == "done"


async def test_pause_resume_cancel(projects: ProjectManager, fake: FakeProvider) -> None:
    project = await projects.create("Etwas Großes")
    pid = project["id"]
    assert projects.control(pid, "pause") is True
    assert (projects.get(pid) or {})["status"] == PAUSED
    assert await projects.step() is False  # pausierte Projekte werden nicht geplant

    assert projects.control(pid, "resume") is True
    assert (projects.get(pid) or {})["status"] == PLANNING  # noch kein Plan
    queue(fake, reply(json.dumps(PLAN)))
    assert await projects.step() is True

    assert projects.control(pid, "pause") is True
    assert await projects.step() is False
    assert projects.control(pid, "resume") is True
    assert (projects.get(pid) or {})["status"] == ACTIVE  # Plan vorhanden

    assert projects.control(pid, "cancel") is True
    assert (projects.get(pid) or {})["status"] == CANCELLED
    assert await projects.step() is False

    assert projects.control(pid, "explode") is False
    assert projects.control("gibtsnicht", "pause") is False
    kinds = [e["kind"] for e in (projects.get(pid) or {})["events"]]
    assert kinds.count("pause") == 2 and "cancel" in kinds


async def test_budget_exhausted_waits_for_user(
    app: JarvisApp, projects: ProjectManager, fake: FakeProvider
) -> None:
    project = await projects.create("Teures Projekt", budget_usd=0.001)
    queue(fake, reply(json.dumps(PLAN), tokens=10_000))  # Planung kostet schon > 0,001 $
    assert await projects.step() is True
    assert app.costs.spent_for_project(project["id"]) > 0.001

    assert await projects.step() is False  # keine Aufgabe ausgeführt
    current = projects.get(project["id"])
    assert current is not None and current["status"] == WAITING
    assert all(t["status"] == "pending" for t in current["tasks"])
    assert "Budget von $0.00 ist aufgebraucht" in current["questions"][0]["question"]
    assert len(fake.requests) == 1


async def test_killswitch_cancels_step_and_requeues_task(
    app: JarvisApp, projects: ProjectManager, fake: FakeProvider
) -> None:
    project = await planned_project(projects, fake)
    fake.delays["fake-big"] = 0.5
    stepper = asyncio.create_task(projects.step())
    await asyncio.sleep(0.1)
    assert task_by_position(projects, project["id"], 1)["status"] == "running"

    app.killswitch.engage("Test")
    assert await asyncio.wait_for(stepper, 2) is True
    assert task_by_position(projects, project["id"], 1)["status"] == "pending"
    app.killswitch.release()


async def test_recover_requeues_interrupted_work(
    projects: ProjectManager, fake: FakeProvider
) -> None:
    project = await planned_project(projects, fake)
    other = await projects.create("Zweites Projekt")
    projects.db.execute(
        "UPDATE project_tasks SET status = 'running' WHERE project_id = ?", (project["id"],)
    )
    projects._set_status(other["id"], "planning_running")

    projects.recover()

    assert all(t["status"] == "pending" for t in (projects.get(project["id"]) or {})["tasks"])
    assert (projects.get(other["id"]) or {})["status"] == PLANNING


async def test_daily_report(app: JarvisApp, projects: ProjectManager, fake: FakeProvider) -> None:
    assert projects.daily_report() == "Heute gab es keine aktiven Projekte."

    project = await planned_project(projects, fake)
    queue(fake, reply("Ideen gesammelt."), verdict(True))
    assert await projects.step() is True
    projects.ask_user(project["id"], "Welche Domain soll ich nehmen?")

    report = projects.daily_report()
    assert "### Website bauen (1/2, active)" in report
    assert "Heute erledigt: Recherche" in report
    assert "Als Nächstes: Umsetzen" in report
    assert "Offene Fragen an dich: Welche Domain soll ich nehmen?" in report
    assert "Kosten: $0.00" in report

    title = f"Tagesbericht {now():%Y-%m-%d}"
    cards = [c for c in app.cards.list() if c["title"] == title]
    assert len(cards) == 1 and cards[0]["content"] == report
    assert (projects.get(project["id"]) or {})["last_report"]

    # Gestern erledigte Projekte tauchen nicht mehr auf.
    projects.db.execute(
        "UPDATE projects SET status = 'done', updated = '2000-01-01T00:00:00+00:00'"
    )
    assert projects.daily_report() == "Heute gab es keine aktiven Projekte."


async def test_background_worker_finishes_project(
    app: JarvisApp, projects: ProjectManager, fake: FakeProvider
) -> None:
    """Ende-zu-Ende über den Worker: Planen, beide Aufgaben, Abschluss."""
    app.config.projects.pause_between_tasks_s = 0
    queue(
        fake,
        reply(json.dumps(PLAN)),
        reply("Recherche fertig."),
        verdict(True),
        reply("Seite fertig."),
        verdict(True),
    )
    projects.start()
    try:
        project = await projects.create("Website")
        for _ in range(200):
            if (projects.get(project["id"]) or {}).get("status") == DONE:
                break
            await asyncio.sleep(0.01)
        current = projects.get(project["id"])
        assert current is not None and current["status"] == DONE
        assert [t["result"] for t in current["tasks"]] == ["Recherche fertig.", "Seite fertig."]
    finally:
        await projects.stop()
    assert projects._workers == []
