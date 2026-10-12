"""Langzeit-Projektmodus: JARVIS arbeitet selbstständig – auch über mehrere Tage.

Ablauf pro Projekt:

1. **Planen** – ein starkes Modell erstellt Meilensteine und Aufgaben (JSON).
2. **Ausführen** – der Agent erledigt Aufgabe für Aufgabe mit allen Werkzeugen.
3. **Prüfen** – ein zweites Modell kontrolliert jedes Ergebnis; bei Mängeln
   wird mit dem Feedback erneut versucht.
4. **Feststecken** – nach mehreren Fehlversuchen oder bei wichtigen
   Entscheidungen fragt JARVIS dich und arbeitet solange an anderem weiter.

Jeder Zwischenstand liegt in SQLite. Nach Neustart oder Absturz geht es genau
dort weiter. Ein Tageslimit schützt die kostenlosen Kontingente.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from jarvis.core.jsonutil import extract_json
from jarvis.core.tasks import spawn
from jarvis.core.timeutil import now, now_iso
from jarvis.core.types import TaskKind
from jarvis.router.base import ChatMessage
from jarvis.router.errors import RouterError

if TYPE_CHECKING:
    from jarvis.core.agent import AgentResult
    from jarvis.core.app import JarvisApp

log = logging.getLogger(__name__)

ACTIVE = "active"
PLANNING = "planning"
PAUSED = "paused"
WAITING = "waiting_user"
DONE = "done"
CANCELLED = "cancelled"

TASK_KINDS = {"chat": TaskKind.CHAT, "code": TaskKind.CODE, "planning": TaskKind.PLANNING,
              "simple": TaskKind.SIMPLE, "research": TaskKind.CHAT, "review": TaskKind.REVIEW}  # fmt: skip

PLANNER_PROMPT = """Du bist der Projektplaner von JARVIS, einem persönlichen KI-Assistenten auf einem
Windows-PC. JARVIS hat Werkzeuge für Dateien, Programme, Internet-Recherche, Bilder, E-Mail, Kalender,
PowerShell (mit Bestätigung) und kann sich mit jarvis_improve selbst neue Fähigkeiten programmieren.

Erstelle für das Projektziel einen realistischen Plan. Antworte NUR mit JSON in diesem Format:
{"title": "kurzer Projekttitel",
 "summary": "2-3 Sätze: Vorgehen und Ergebnis",
 "milestones": [
   {"title": "Meilenstein", "tasks": [
     {"title": "konkrete Aufgabe", "description": "was genau zu tun ist und woran man erkennt, dass es fertig ist",
      "kind": "chat|research|code|planning|simple", "depends_on": [Nummern vorheriger Aufgaben, ab 1 gezählt]}
   ]}
 ]}
Regeln: 3-15 Aufgaben insgesamt, jede in einem Arbeitsschritt erledigbar, Abhängigkeiten nur auf
frühere Aufgaben. "code" für Programmieraufgaben, "research" für Recherche."""

VERIFIER_PROMPT = """Du prüfst Arbeitsergebnisse für JARVIS. Bewerte streng, aber fair, ob die Aufgabe
erfüllt ist. Antworte NUR mit JSON: {"ok": true|false, "feedback": "konkret, was fehlt oder falsch ist"}"""


class ProjectManager:
    def __init__(self, app: JarvisApp) -> None:
        self.app = app
        self.db = app.db
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._workers: list[asyncio.Task[None]] = []

    # ------------------------------------------------------------------ Abfragen

    def _counts(self, project_id: str) -> tuple[int, int]:
        row = self.db.query_one(
            "SELECT COUNT(*), SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) "
            "FROM project_tasks WHERE project_id = ?",
            (project_id,),
        )
        return (int(row[1] or 0), int(row[0] or 0)) if row else (0, 0)

    def list(self) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM projects ORDER BY status = 'done', priority, updated DESC"
        )
        result = []
        for row in rows:
            done, total = self._counts(row["id"])
            open_questions = self.db.query_one(
                "SELECT COUNT(*) FROM project_questions WHERE project_id = ? AND status = 'open'",
                (row["id"],),
            )
            result.append(
                dict(row)
                | {
                    "done": done,
                    "total": total,
                    "open_questions": int(open_questions[0]) if open_questions else 0,
                    "cost_usd": self.app.costs.spent_for_project(row["id"]),
                }
            )
        return result

    def get(self, project_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
        if row is None:
            return None
        tasks = [
            dict(r)
            for r in self.db.query(
                "SELECT * FROM project_tasks WHERE project_id = ? ORDER BY position", (project_id,)
            )
        ]
        questions = [
            dict(r)
            for r in self.db.query(
                "SELECT * FROM project_questions WHERE project_id = ? ORDER BY id DESC",
                (project_id,),
            )
        ]
        events = [
            dict(r)
            for r in self.db.query(
                "SELECT * FROM project_events WHERE project_id = ? ORDER BY id DESC LIMIT 100",
                (project_id,),
            )
        ]
        done, total = self._counts(project_id)
        return dict(row) | {
            "tasks": tasks,
            "questions": questions,
            "events": events,
            "done": done,
            "total": total,
            "plan": json.loads(row["plan"]) if row["plan"] else None,
            "cost_usd": self.app.costs.spent_for_project(project_id),
        }

    def status_text(self, project_id: str) -> str | None:
        project = self.get(project_id)
        if project is None:
            return None
        lines = [
            f"Projekt #{project['id']}: {project['title']} – {project['status']} ({project['done']}/{project['total']})"
        ]
        if project.get("summary"):
            lines.append(project["summary"])
        for task in project["tasks"]:
            mark = {"done": "✔", "running": "▶", "failed": "✘", "blocked": "⚠", WAITING: "?"}.get(
                task["status"], "·"
            )
            lines.append(f"{mark} {task['position']}. {task['title']}")
        for question in project["questions"]:
            if question["status"] == "open":
                lines.append(f"Offene Frage #{question['id']}: {question['question']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ Änderungen

    def _event(self, project_id: str, kind: str, text: str) -> None:
        self.db.execute(
            "INSERT INTO project_events (project_id, ts, kind, text) VALUES (?, ?, ?, ?)",
            (project_id, now_iso(), kind, text[:4000]),
        )
        self.app.events.publish(
            "project.updated", {"id": project_id, "kind": kind, "text": text[:300]}
        )

    def _set_status(self, project_id: str, status: str) -> None:
        self.db.execute(
            "UPDATE projects SET status = ?, updated = ? WHERE id = ?",
            (status, now_iso(), project_id),
        )

    async def create(
        self, goal: str, *, title: str | None = None, priority: int = 3, budget_usd: float = 0.0
    ) -> dict[str, Any]:
        project_id = uuid.uuid4().hex[:8]
        stamp = now_iso()
        self.db.execute(
            "INSERT INTO projects (id, title, goal, status, priority, budget_usd, created, updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                title or goal.strip().splitlines()[0][:60],
                goal,
                PLANNING,
                max(1, min(priority, 5)),
                budget_usd,
                stamp,
                stamp,
            ),
        )
        self._event(project_id, "created", f"Projekt angelegt: {goal[:300]}")
        self.wake()
        project = self.get(project_id)
        assert project is not None
        return project

    def control(self, project_id: str, action: str) -> bool:
        project = self.db.query_one("SELECT status FROM projects WHERE id = ?", (project_id,))
        if project is None:
            return False
        if action == "pause":
            self._set_status(project_id, PAUSED)
        elif action == "resume":
            has_plan = self.db.query_one("SELECT plan FROM projects WHERE id = ?", (project_id,))
            self._set_status(project_id, ACTIVE if has_plan and has_plan[0] else PLANNING)
            self.wake()
        elif action == "cancel":
            self._set_status(project_id, CANCELLED)
        else:
            return False
        self._event(project_id, action, f"Projekt: {action}")
        return True

    def ask_user(self, project_id: str, question: str, task_id: int | None = None) -> int:
        question_id = self.db.execute(
            "INSERT INTO project_questions (project_id, task_id, question, created) VALUES (?, ?, ?, ?)",
            (project_id, task_id, question, now_iso()),
        )
        self._event(project_id, "question", question)
        title_row = self.db.query_one("SELECT title FROM projects WHERE id = ?", (project_id,))
        self.app.notify(
            f"Frage zum Projekt {title_row[0] if title_row else project_id}",
            question,
            speak=False,
        )
        return question_id

    def answer(self, question_id: int, answer: str) -> bool:
        row = self.db.query_one(
            "SELECT * FROM project_questions WHERE id = ? AND status = 'open'", (question_id,)
        )
        if row is None:
            return False
        self.db.execute(
            "UPDATE project_questions SET answer = ?, status = 'answered', answered = ? WHERE id = ?",
            (answer, now_iso(), question_id),
        )
        skip = answer.strip().lower() in {"überspringen", "ueberspringen", "skip", "weglassen"}
        if row["task_id"] is not None and skip:
            self.db.execute(
                "UPDATE project_tasks SET status = 'skipped', updated = ? WHERE id = ?",
                (now_iso(), row["task_id"]),
            )
        elif row["task_id"] is not None:
            self.db.execute(
                "UPDATE project_tasks SET status = 'pending', attempts = 0, feedback = ?, updated = ? "
                "WHERE id = ? AND status IN ('waiting_user', 'blocked')",
                (
                    f"Antwort des Benutzers auf '{row['question']}': {answer}",
                    now_iso(),
                    row["task_id"],
                ),
            )
        status = self.db.query_one(
            "SELECT status, plan FROM projects WHERE id = ?", (row["project_id"],)
        )
        if status is not None and status["status"] == WAITING:
            self._set_status(row["project_id"], ACTIVE if status["plan"] else PLANNING)
        self._event(row["project_id"], "answer", f"Antwort: {answer}")
        self.wake()
        return True

    # ------------------------------------------------------------------ Worker

    def wake(self) -> None:
        self._wake.set()

    def recover(self) -> None:
        """Nach Neustart/Absturz: unterbrochene Aufgaben wieder einreihen."""
        self.db.execute("UPDATE project_tasks SET status = 'pending' WHERE status = 'running'")
        self.db.execute(
            "UPDATE projects SET status = ? WHERE status = 'planning_running'", (PLANNING,)
        )

    def start(self) -> None:
        self.recover()
        for index in range(self.app.config.projects.max_parallel):
            task = asyncio.create_task(self._worker(index), name=f"projekt-worker-{index}")
            self._workers.append(task)

    async def stop(self) -> None:
        for task in self._workers:
            task.cancel()
        for task in self._workers:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._workers.clear()

    def _tasks_today(self) -> int:
        key = f"project_tasks:{now():%Y-%m-%d}"
        return int(self.db.get_state(key, "0") or 0)

    def _count_task(self) -> None:
        key = f"project_tasks:{now():%Y-%m-%d}"
        self.db.set_state(key, str(self._tasks_today() + 1))

    async def _worker(self, index: int) -> None:
        config = self.app.config.projects
        while True:
            try:
                if self.app.killswitch.engaged or self._tasks_today() >= config.max_tasks_per_day:
                    await asyncio.sleep(60)
                    continue
                worked = await self.step()
                if worked:
                    await asyncio.sleep(config.pause_between_tasks_s)
                    continue
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), 60)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Projekt-Worker %s: unerwarteter Fehler", index)
                await asyncio.sleep(30)

    async def step(self) -> bool:
        """Erledigt einen Arbeitsschritt. ``False`` = nichts zu tun."""
        async with self._lock:
            planning = self.db.query_one(
                "SELECT * FROM projects WHERE status = ? ORDER BY priority, created LIMIT 1",
                (PLANNING,),
            )
            if planning is not None:
                self._set_status(planning["id"], "planning_running")
                target: tuple[str, Any] = ("plan", planning)
            else:
                task = self._next_task()
                if task is None:
                    return False
                self.db.execute(
                    "UPDATE project_tasks SET status = 'running', updated = ? WHERE id = ?",
                    (now_iso(), task["id"]),
                )
                target = ("task", task)
        kind, row = target
        job = asyncio.create_task(
            self._plan(dict(row)) if kind == "plan" else self._run_task(dict(row)),
            name=f"projekt-{kind}",
        )
        # Der Not-Aus bricht nur diesen Schritt ab – der Worker selbst läuft weiter.
        self.app.killswitch.track(job)
        try:
            await job
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                job.cancel()
                raise
            log.info("Projektschritt abgebrochen (Not-Aus)")
            if kind == "plan":
                self._set_status(row["id"], PLANNING)
            else:
                self.db.execute(
                    "UPDATE project_tasks SET status = 'pending' WHERE id = ? AND status = 'running'",
                    (row["id"],),
                )
            return True
        self._count_task()
        return True

    def _next_task(self) -> Any:
        projects = self.db.query(
            "SELECT * FROM projects WHERE status = ? ORDER BY priority, updated", (ACTIVE,)
        )
        for project in projects:
            if (
                project["budget_usd"] > 0
                and self.app.costs.spent_for_project(project["id"]) >= project["budget_usd"]
            ):
                self._set_status(project["id"], WAITING)
                self.ask_user(
                    project["id"],
                    f"Das Budget von ${project['budget_usd']:.2f} ist aufgebraucht. Soll ich mit "
                    "kostenlosen Modellen weitermachen oder das Budget erhöhen?",
                )
                continue
            tasks = self.db.query(
                "SELECT * FROM project_tasks WHERE project_id = ? ORDER BY position",
                (project["id"],),
            )
            done_positions = {t["position"] for t in tasks if t["status"] in ("done", "skipped")}
            for task in tasks:
                if task["status"] != "pending":
                    continue
                depends = json.loads(task["depends_on"] or "[]")
                if all(d in done_positions for d in depends):
                    return task
            if tasks and all(t["status"] in ("done", "skipped") for t in tasks):
                spawn(self._finish(dict(project)), name="project-finish")
        return None

    async def _plan(self, project: dict[str, Any]) -> None:
        project_id = project["id"]
        self._event(project_id, "planning", "Plane das Projekt …")
        try:
            response = await self.app.router.complete(
                [ChatMessage(role="user", content=f"Projektziel:\n{project['goal']}")],
                task=TaskKind.PLANNING,
                system=PLANNER_PROMPT,
                project_id=project_id,
            )
            plan = extract_json(response.text)
            milestones = plan.get("milestones") or []
            if not milestones:
                raise ValueError("Plan enthält keine Meilensteine")
        except (RouterError, ValueError, AttributeError) as exc:
            self._event(project_id, "error", f"Planung fehlgeschlagen: {exc}")
            self._set_status(project_id, WAITING)
            self.ask_user(
                project_id,
                f"Ich konnte keinen Plan erstellen ({exc}). Kannst du das Ziel genauer beschreiben?",
            )
            return
        stamp = now_iso()
        position = 0
        for milestone in milestones:
            for task in milestone.get("tasks") or []:
                position += 1
                depends = [
                    int(d)
                    for d in task.get("depends_on") or []
                    if str(d).isdigit() and int(d) < position
                ]
                self.db.execute(
                    "INSERT INTO project_tasks (project_id, position, milestone, title, description, kind, "
                    "depends_on, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        project_id,
                        position,
                        str(milestone.get("title", "")),
                        str(task.get("title", f"Aufgabe {position}")),
                        str(task.get("description", "")),
                        str(task.get("kind", "chat")),
                        json.dumps(depends),
                        stamp,
                        stamp,
                    ),
                )
        self.db.execute(
            "UPDATE projects SET title = ?, plan = ?, summary = ?, status = ?, updated = ? WHERE id = ?",
            (
                str(plan.get("title") or project["title"])[:80],
                json.dumps(plan, ensure_ascii=False),
                str(plan.get("summary", "")),
                ACTIVE,
                now_iso(),
                project_id,
            ),
        )
        self._event(project_id, "planned", f"Plan erstellt: {position} Aufgaben")

    def _context(self, project_id: str, task: dict[str, Any]) -> str:
        project = self.get(project_id) or {}
        done = [t for t in project.get("tasks", []) if t["status"] == "done"][-10:]
        answers = [q for q in project.get("questions", []) if q["status"] == "answered"][:10]
        folder = self.app.workspace / "projekte" / project_id
        folder.mkdir(parents=True, exist_ok=True)
        lines = [
            f"Du arbeitest selbstständig am Projekt '{project.get('title')}'.",
            f"Projektziel: {project.get('goal')}",
            f"Plan: {project.get('summary') or ''}",
            f"Projektordner für Dateien: {folder}",
        ]
        if done:
            lines.append("Bereits erledigt:")
            lines += [f"- {t['position']}. {t['title']}: {(t['result'] or '')[:400]}" for t in done]
        if answers:
            lines.append("Antworten des Benutzers:")
            lines += [f"- {q['question']} → {q['answer']}" for q in answers]
        lines += [
            "",
            f"AKTUELLE AUFGABE {task['position']}: {task['title']}",
            task.get("description") or "",
        ]
        if task.get("feedback"):
            lines.append(f"Hinweis aus dem letzten Versuch: {task['feedback']}")
        lines.append(
            "Erledige die Aufgabe mit deinen Werkzeugen. Speichere Ergebnisse als Dateien im "
            "Projektordner. Fehlt dir eine Fähigkeit, nutze jarvis_improve. Steckst du fest oder ist "
            "eine wichtige Entscheidung nötig, nutze project_ask_user. Schließe mit einer kurzen "
            "Zusammenfassung des Ergebnisses ab."
        )
        return "\n".join(lines)

    async def _run_task(self, task: dict[str, Any]) -> None:
        project_id = task["project_id"]
        attempts = int(task["attempts"]) + 1
        self._event(
            project_id,
            "task",
            f"Starte Aufgabe {task['position']}: {task['title']} (Versuch {attempts})",
        )
        session = f"projekt-{project_id}"
        kind = TASK_KINDS.get(str(task["kind"]), TaskKind.CHAT)
        started = now_iso()
        result: AgentResult | None = None
        error: str | None = None
        try:
            result = await self.app.agent.ask(
                session,
                self._context(project_id, task),
                task=kind,
                source="project",
                project_id=project_id,
                persist=False,
            )
        except asyncio.CancelledError:
            self.db.execute(
                "UPDATE project_tasks SET status = 'pending' WHERE id = ?", (task["id"],)
            )
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        if result is not None and result.error:
            error = result.error
        if error or result is None:
            self._retry_or_block(task, attempts, f"Fehler bei der Ausführung: {error}")
            return
        waiting = self.db.query_one(
            "SELECT id FROM project_questions WHERE project_id = ? AND status = 'open' AND created >= ?",
            (project_id, started),
        )
        if waiting is not None:
            self.db.execute(
                "UPDATE project_questions SET task_id = ? WHERE id = ?", (task["id"], waiting[0])
            )
            self.db.execute(
                "UPDATE project_tasks SET status = 'waiting_user', result = ?, attempts = ?, updated = ? WHERE id = ?",
                (result.text, attempts, now_iso(), task["id"]),
            )
            self._event(
                project_id, "waiting", f"Aufgabe {task['position']} wartet auf deine Antwort"
            )
            return
        ok, feedback = await self._verify(task, result.text)
        if ok:
            self.db.execute(
                "UPDATE project_tasks SET status = 'done', result = ?, attempts = ?, feedback = NULL, updated = ? WHERE id = ?",
                (result.text, attempts, now_iso(), task["id"]),
            )
            self._event(project_id, "done", f"Aufgabe {task['position']} erledigt: {task['title']}")
        else:
            self.db.execute(
                "UPDATE project_tasks SET result = ? WHERE id = ?", (result.text, task["id"])
            )
            self._retry_or_block(task, attempts, feedback)

    def _retry_or_block(self, task: dict[str, Any], attempts: int, feedback: str) -> None:
        project_id = task["project_id"]
        if attempts >= self.app.config.projects.max_task_attempts:
            self.db.execute(
                "UPDATE project_tasks SET status = 'blocked', attempts = ?, feedback = ?, updated = ? WHERE id = ?",
                (attempts, feedback, now_iso(), task["id"]),
            )
            self.ask_user(
                project_id,
                f"Aufgabe {task['position']} ('{task['title']}') klappt nach {attempts} Versuchen nicht: "
                f"{feedback[:400]} – Wie soll ich vorgehen? (Antwort hilft mir weiter, 'überspringen' überspringt sie.)",
                task_id=task["id"],
            )
        else:
            self.db.execute(
                "UPDATE project_tasks SET status = 'pending', attempts = ?, feedback = ?, updated = ? WHERE id = ?",
                (attempts, feedback, now_iso(), task["id"]),
            )
            self._event(
                project_id, "retry", f"Aufgabe {task['position']} wird verbessert: {feedback[:300]}"
            )

    async def _verify(self, task: dict[str, Any], result_text: str) -> tuple[bool, str]:
        """Selbstkontrolle durch ein zweites Modell (Route ``review``)."""
        prompt = (
            f"Aufgabe: {task['title']}\nBeschreibung: {task.get('description') or ''}\n\n"
            f"Ergebnis des Bearbeiters:\n{result_text[:8000]}"
        )
        try:
            response = await self.app.router.complete(
                [ChatMessage(role="user", content=prompt)],
                task=TaskKind.REVIEW,
                system=VERIFIER_PROMPT,
                project_id=task["project_id"],
            )
            verdict = extract_json(response.text)
            return bool(verdict.get("ok")), str(verdict.get("feedback") or "")
        except (RouterError, ValueError, AttributeError) as exc:
            log.info("Prüfung nicht möglich (%s) – Ergebnis wird akzeptiert", exc)
            return True, ""

    async def _finish(self, project: dict[str, Any]) -> None:
        project_id = project["id"]
        current = self.db.query_one("SELECT status FROM projects WHERE id = ?", (project_id,))
        if current is None or current[0] != ACTIVE:
            return
        self._set_status(project_id, DONE)
        self._event(project_id, "finished", "Projekt abgeschlossen 🎉")
        self.app.notify("Projekt abgeschlossen", f"{project['title']} ist fertig.", speak=True)

    # ------------------------------------------------------------------ Berichte

    def daily_report(self, day: datetime | None = None) -> str:
        """Tagesbericht: erledigt, als Nächstes, offene Fragen, Probleme, Kosten."""
        today = (day or now()).strftime("%Y-%m-%d")
        sections = []
        for project in self.list():
            if project["status"] in (DONE, CANCELLED) and not str(project["updated"]).startswith(
                today
            ):
                continue
            details = self.get(project["id"]) or {}
            done_today = [
                t
                for t in details.get("tasks", [])
                if t["status"] == "done" and str(t["updated"]).startswith(today)
            ]
            upcoming = [t for t in details.get("tasks", []) if t["status"] == "pending"][:3]
            problems = [t for t in details.get("tasks", []) if t["status"] in ("blocked", "failed")]
            questions = [q for q in details.get("questions", []) if q["status"] == "open"]
            lines = [
                f"### {project['title']} ({project['done']}/{project['total']}, {project['status']})"
            ]
            lines.append(
                "Heute erledigt: " + (", ".join(t["title"] for t in done_today) or "nichts")
            )
            if upcoming:
                lines.append("Als Nächstes: " + ", ".join(t["title"] for t in upcoming))
            if problems:
                lines.append(
                    "Probleme: "
                    + ", ".join(f"{t['title']} ({(t['feedback'] or '')[:80]})" for t in problems)
                )
            if questions:
                lines.append(
                    "Offene Fragen an dich: " + " | ".join(q["question"][:120] for q in questions)
                )
            lines.append(f"Kosten: ${project['cost_usd']:.2f}")
            sections.append("\n".join(lines))
            self.db.execute(
                "UPDATE projects SET last_report = ? WHERE id = ?", (now_iso(), project["id"])
            )
        report = "\n\n".join(sections) if sections else "Heute gab es keine aktiven Projekte."
        self.app.cards.upsert(f"Tagesbericht {today}", report)
        self.app.events.publish("cards.changed", {})
        return report
