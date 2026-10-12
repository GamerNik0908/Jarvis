"""Selbstverbesserung mit Claude Code – kontrolliert, getestet, mit deiner Freigabe.

Ablauf einer Verbesserung ("Bau mir eine Druckfunktion"):

1. Eigener Git-Branch + Worktree (Kopie des Codes) – der laufende Code bleibt unberührt.
2. Claude Code (über dein Pro-Abo) programmiert dort die Änderung inkl. Tests.
   Den Zwischenstand siehst du live im Dashboard.
3. Schutzprüfung: Änderungen an Sicherheitscode, Selbstverbesserung oder
   geschützten Konfigurationsabschnitten werden verworfen.
4. Tests (pytest). Schlagen sie fehl, bekommt Claude Code die Fehler zurück und
   bessert nach (``self_improve.max_fix_rounds``).
5. Du bekommst Zusammenfassung + Diff und klickst "Übernehmen" (oder "Verwerfen").
6. Merge, CHANGELOG-Eintrag, Neustart. Startet die neue Version nicht sauber,
   rollt der Supervisor automatisch auf den vorherigen Stand zurück.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from collections.abc import AsyncGenerator, Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.core.config import PROJECT_ROOT
from jarvis.core.jsonutil import extract_json
from jarvis.core.timeutil import now_iso
from jarvis.integrations.claude_code import CCEvent, ClaudeCodeCLI, ClaudeCodeError, ClaudeCodeRun
from jarvis.router.providers.claude_code_provider import ClaudeCodeProvider
from jarvis.security import guard
from jarvis.self_improve import git
from jarvis.self_improve.sandbox import REQUIREMENT_FILES, Sandbox, base_python, run_tests

if TYPE_CHECKING:
    from jarvis.core.app import JarvisApp

log = logging.getLogger(__name__)

Runner = Callable[[ClaudeCodeRun], AsyncGenerator[CCEvent, None]]

UPDATE_MARKER = "update_pending.json"
RESTART_EXIT_CODE = 75
#: Wie weit der Selbst-Check im Aktionsprotokoll nach fehlgeschlagenen Werkzeugen sucht.
PROBLEM_LOOKBACK_DAYS = 7

DEV_SYSTEM = """Du arbeitest am Quellcode von JARVIS, einem persönlichen KI-Assistenten (Python 3.11+,
Windows 11). Du befindest dich in einem eigenen Git-Worktree – der laufende JARVIS ist davon unberührt.

Regeln:
- Lies zuerst docs/ARCHITECTURE.md und die betroffenen Module. Halte dich an Stil und Struktur:
  Type Hints, deutsche Docstrings/Kommentare, englische Bezeichner, Konfiguration über config.yaml.
- Neue Fähigkeiten als Plugin in jarvis/plugins/ (siehe jarvis/plugins/base.py, @tool-Dekorator).
  Riskante Aktionen bekommen level=Level.CONFIRM.
- Schreibe Tests in tests/ und führe sie aus: python -m pytest -q
  Shell-Befehle einzeln und ohne cd, Pipes, && oder Umleitungen ausführen – nur so sind sie
  freigegeben (erlaubt: python -m pytest/ruff/mypy, node --check, git status/diff/log).
- Ändere NIEMALS: jarvis/security/, jarvis/self_improve/, jarvis/core/supervisor.py, .env,
  config.local.yaml und in config.yaml die Abschnitte security, budget, secrets, self_improve.
  Solche Änderungen werden automatisch verworfen.
- Installiere keine Pakete. Neue Abhängigkeiten nur in requirements.txt (und pyproject.toml)
  eintragen – JARVIS installiert sie nach Freigabe des Benutzers.
- Committe nicht selbst und bearbeite CHANGELOG.md nicht – JARVIS übernimmt beides.
- Beende deine Arbeit mit einer kurzen deutschen Zusammenfassung für den Benutzer: was neu ist,
  wie man es benutzt, was getestet wurde."""

DEV_ALLOWED_TOOLS = [
    "Read",
    "Edit",
    "Write",
    "Glob",
    "Grep",
    "WebSearch",
    "WebFetch",
    "TodoWrite",
    "Bash(python -m pytest *)",
    "Bash(python -m ruff *)",
    "Bash(python -m mypy *)",
    "Bash(git status *)",
    "Bash(git diff *)",
    "Bash(git log *)",
    "Bash(node --check *)",
    "PowerShell(python -m pytest *)",
    "PowerShell(python -m ruff *)",
    "PowerShell(python -m mypy *)",
    "PowerShell(node --check *)",
]

SELF_CHECK_PROMPT = """Prüfe JARVIS auf sinnvolle Verbesserungen. Du darfst nur lesen.

Fehler und Probleme der letzten Tage:
{problems}

Schlage höchstens 3 konkrete, kleine, gut testbare Verbesserungen vor (Fehlerbehebungen haben Vorrang
vor neuen Funktionen). Antworte NUR mit JSON:
[{{"title": "kurzer Titel", "description": "genaue Beschreibung der Änderung für einen Entwickler"}}]
Wenn es nichts Sinnvolles gibt: []"""


_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower().translate(_UMLAUTS)).strip("-")
    return slug[:30] or "aenderung"


def _protected_disallow_rules() -> list[str]:
    rules = []
    for path in guard.PROTECTED_PATHS:
        if path.startswith(".git"):
            continue
        pattern = f"{path}**" if path.endswith("/") else path
        rules += [
            f"Edit({pattern})",
            f"Write({pattern})",
            f"Edit(./{pattern})",
            f"Write(./{pattern})",
        ]
    rules += ["Bash(git push *)", "Bash(git reset *)", "Bash(git checkout *)", "Bash(pip *)",
              "Bash(python -m pip *)", "PowerShell(pip *)", "PowerShell(python -m pip *)"]  # fmt: skip
    return rules


class ImprovementManager:
    def __init__(
        self,
        app: JarvisApp,
        *,
        repo: Path = PROJECT_ROOT,
        runner: Runner | None = None,
        restart: Callable[[], None] | None = None,
    ) -> None:
        self.app = app
        self.db = app.db
        self.repo = repo
        self.base_dir = app.data_dir / "improvements"
        self.worktrees = app.data_dir / "worktrees"
        self.sandbox = Sandbox(app.data_dir / "sandbox")
        provider = app.provider_of_type(ClaudeCodeProvider)
        self._provider: ClaudeCodeProvider | None = provider
        self._cli = provider.cli if provider is not None else ClaudeCodeCLI()
        self._runner: Runner = runner or self._cli.stream
        self._restart = restart
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._current: asyncio.Task[None] | None = None
        self._repo_ok: bool | None = None

    # ------------------------------------------------------------------ Status

    def unavailable_reason(self) -> str | None:
        if not self.app.config.self_improve.enabled:
            return "Selbstverbesserung ist ausgeschaltet"
        if git.git_executable() is None:
            return "Git fehlt – 'winget install Git.Git'"
        if self._repo_ok is False:
            return f"{self.repo} ist kein Git-Repository (JARVIS per 'git clone' installieren)"
        if self._provider is not None:
            reason = self._provider.unavailable_reason()
            if reason:
                return reason
        elif self._cli.executable is None:
            return "Claude Code nicht installiert – 'winget install Anthropic.ClaudeCode'"
        return None

    async def check_repo(self) -> bool:
        self._repo_ok = await git.is_repo(self.repo)
        return self._repo_ok

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM improvements ORDER BY id DESC LIMIT ?", (limit,))
        return [self._row(r) for r in rows]

    def get(self, job_id: int) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM improvements WHERE id = ?", (job_id,))
        return self._row(row) if row else None

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        data = dict(row)
        data["changed_files"] = (
            json.loads(data["changed_files"]) if data.get("changed_files") else []
        )
        return data

    def log_path(self, job_id: int) -> Path:
        return self.base_dir / f"{job_id}.log"

    def read_log(self, job_id: int, tail: int = 400) -> list[str]:
        path = self.log_path(job_id)
        if not path.is_file():
            return []
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-tail:]

    async def diff(self, job_id: int) -> str:
        job = self.get(job_id)
        if job is None or not job.get("base_commit") or not job.get("branch"):
            return ""
        try:
            return await git.diff_text(self.repo, job["base_commit"], job["branch"])
        except git.GitError as exc:
            return f"(Diff nicht verfügbar: {exc})"

    def suggestions(self) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM improvement_suggestions WHERE status = 'open' ORDER BY id DESC LIMIT 20"
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ Aktionen

    def _update(self, job_id: int, **fields: Any) -> None:
        fields["updated"] = now_iso()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        self.db.execute(
            f"UPDATE improvements SET {assignments} WHERE id = ?", (*fields.values(), job_id)
        )
        job = self.get(job_id)
        self.app.events.publish(
            "improvement.updated", {"id": job_id, "status": job["status"] if job else None}
        )

    def _log(self, job_id: int, line: str) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%H:%M:%S")
        with self.log_path(job_id).open("a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {line}\n")
        self.app.events.publish("improvement.log", {"id": job_id, "line": f"[{stamp}] {line}"})

    def request(self, text: str, *, source: str = "user") -> dict[str, Any]:
        stamp = now_iso()
        job_id = self.db.execute(
            "INSERT INTO improvements (request, source, status, created, updated) VALUES (?, ?, 'queued', ?, ?)",
            (text.strip(), source, stamp, stamp),
        )
        self.app.events.publish("improvement.updated", {"id": job_id, "status": "queued"})
        self._wake.set()
        job = self.get(job_id)
        assert job is not None
        return job

    def implement_suggestion(self, suggestion_id: int) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM improvement_suggestions WHERE id = ?", (suggestion_id,)
        )
        if row is None:
            return None
        self.db.execute(
            "UPDATE improvement_suggestions SET status = 'accepted' WHERE id = ?", (suggestion_id,)
        )
        return self.request(f"{row['title']}: {row['description']}", source="self_check")

    def dismiss_suggestion(self, suggestion_id: int) -> None:
        self.db.execute(
            "UPDATE improvement_suggestions SET status = 'dismissed' WHERE id = ?", (suggestion_id,)
        )

    async def cancel(self, job_id: int) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        if job["status"] == "queued":
            self._update(job_id, status="cancelled")
            return True
        if job["status"] in ("running", "testing") and self._current is not None:
            self._current.cancel()
            return True
        return False

    async def reject(self, job_id: int) -> bool:
        job = self.get(job_id)
        if job is None or job["status"] not in ("ready", "failed"):
            return False
        await self._cleanup(job, delete_branch=True)
        self._update(job_id, status="rejected")
        self._log(job_id, "Verworfen.")
        return True

    async def _cleanup(self, job: dict[str, Any], *, delete_branch: bool) -> None:
        if job.get("worktree"):
            await git.worktree_remove(self.repo, Path(job["worktree"]))
        if delete_branch and job.get("branch"):
            await git.delete_branch(self.repo, job["branch"])

    # ------------------------------------------------------------------ Worker

    def start(self) -> None:
        self.db.execute(
            "UPDATE improvements SET status = 'queued', updated = ? WHERE status IN ('running', 'testing')",
            (now_iso(),),
        )
        self._worker = asyncio.create_task(self._loop(), name="selbstverbesserung")

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._worker

    async def _loop(self) -> None:
        await self.check_repo()
        while True:
            row = self.db.query_one(
                "SELECT * FROM improvements WHERE status = 'queued' ORDER BY id LIMIT 1"
            )
            if row is None or self.unavailable_reason() or self.app.killswitch.engaged:
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), 60)
                continue
            job = self._row(row)
            self._current = asyncio.create_task(self.process(job), name=f"verbesserung-{job['id']}")
            self.app.killswitch.track(self._current)
            try:
                await self._current
            except asyncio.CancelledError:
                if asyncio.current_task() is not None and asyncio.current_task().cancelling():  # type: ignore[union-attr]
                    raise
                self._log(job["id"], "Abgebrochen.")
                current = self.get(job["id"])
                if current is not None:
                    await self._cleanup(current, delete_branch=True)
                self._update(job["id"], status="cancelled")
            except Exception as exc:
                log.exception("Selbstverbesserung #%s fehlgeschlagen", job["id"])
                self._log(job["id"], f"Fehler: {exc}")
                self._update(job["id"], status="failed", error=str(exc)[:2000])
            finally:
                self._current = None

    async def process(self, job: dict[str, Any]) -> None:
        job_id = int(job["id"])
        config = self.app.config.self_improve
        self.log_path(job_id).unlink(missing_ok=True)
        self._log(job_id, f"Auftrag: {job['request']}")
        base = await git.head(self.repo)
        branch = f"jarvis/verbesserung-{job_id}-{_slug(job['request'])}"
        worktree = self.worktrees / str(job_id)
        await git.worktree_add(self.repo, worktree, branch, base)
        self._update(
            job_id, status="running", branch=branch, worktree=str(worktree), base_commit=base
        )
        self._log(job_id, f"Arbeitskopie angelegt (Branch {branch}).")

        session_id = await self._claude(
            job_id, worktree, self._initial_prompt(job["request"]), resume=None
        )
        rounds = 0
        while True:
            changed = await self._commit_and_check(job_id, worktree, base, rounds)
            if changed is None:
                return
            self._update(job_id, status="testing")
            ok, output = await self._test(job_id, worktree, base, changed)
            self._update(job_id, test_output=output[-6000:])
            if ok:
                break
            if rounds >= config.max_fix_rounds or session_id is None:
                self._update(
                    job_id, status="failed", error="Tests schlagen fehl – siehe Testausgabe"
                )
                self.app.notify(
                    "Selbstverbesserung",
                    f"#{job_id}: Tests schlagen fehl, Änderung nicht übernommen.",
                )
                return
            rounds += 1
            self._update(job_id, status="running")
            self._log(job_id, f"Tests fehlgeschlagen – Claude Code bessert nach (Runde {rounds}).")
            session_id = await self._claude(
                job_id,
                worktree,
                "Die Tests schlagen fehl. Bitte analysiere und behebe die Ursache (nicht die Tests "
                f"abschwächen):\n\n{output[-6000:]}",
                resume=session_id,
            )

        stat = await git.diff_stat(worktree, base)
        self._update(job_id, status="ready", diff_stat=stat, result_commit=await git.head(worktree))
        self._log(job_id, "Fertig und getestet – wartet auf deine Freigabe.")
        current = self.get(job_id) or {}
        self.app.notify(
            "Verbesserung bereit",
            f"#{job_id}: {(current.get('summary') or job['request'])[:160]}",
            speak=False,
        )
        if self._auto_merge_allowed(current.get("changed_files") or []):
            self._log(job_id, "Automatische Übernahme laut Einstellung.")
            await self.approve(job_id)

    def _initial_prompt(self, request: str) -> str:
        return (
            f"Auftrag des Benutzers:\n{request}\n\n"
            "Setze das vollständig um (Code, Tests, ggf. Konfiguration und README-Hinweis). "
            "Führe danach die Tests aus und behebe Fehler."
        )

    async def _claude(
        self, job_id: int, worktree: Path, prompt: str, *, resume: str | None
    ) -> str | None:
        config = self.app.config.self_improve
        run = ClaudeCodeRun(
            prompt=prompt,
            cwd=worktree,
            model=config.claude_model,
            append_system_prompt=DEV_SYSTEM,
            tools=None,
            allowed_tools=DEV_ALLOWED_TOOLS,
            disallowed_tools=_protected_disallow_rules(),
            permission_mode="acceptEdits",
            max_turns=config.max_turns,
            resume_session=resume,
            keep_session=True,
            timeout_s=3600,
            extra_env={
                "PATH": str(Path(base_python()).parent) + os.pathsep + os.environ.get("PATH", "")
            },
        )
        session_id = resume
        buffer = ""
        result_text = ""
        try:
            async for event in self._runner(run):
                if event.kind == "init":
                    session_id = str(event.raw.get("session_id") or session_id or "") or None
                    self._log(
                        job_id,
                        f"Claude Code gestartet ({event.raw.get('model', config.claude_model)}).",
                    )
                elif event.kind == "text":
                    buffer += event.text
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        if line.strip():
                            self._log(job_id, f"💬 {line.strip()}")
                elif event.kind == "tool_use":
                    self._log(
                        job_id, f"🔧 {event.tool_name}: {_describe_tool(event.tool_input or {})}"
                    )
                elif event.kind == "status":
                    self._log(job_id, event.text)
                elif event.kind == "result" and event.result is not None:
                    result = event.result
                    session_id = result.session_id or session_id
                    result_text = result.text
                    if result.is_error:
                        raise ClaudeCodeError(
                            result.text or "Claude Code meldet einen Fehler",
                            kind=result.error_kind or "unknown",
                        )
                    for denial in result.permission_denials[:5]:
                        self._log(job_id, f"⛔ Nicht erlaubt: {_describe_denial(denial)}")
        finally:
            if buffer.strip():
                self._log(job_id, f"💬 {buffer.strip()}")
        if result_text:
            self._update(job_id, summary=result_text[-4000:], session_id=session_id)
        return session_id

    async def _commit_and_check(
        self, job_id: int, worktree: Path, base: str, rounds: int
    ) -> list[str] | None:
        message = f"JARVIS-Verbesserung #{job_id}" + (
            f" (Nachbesserung {rounds})" if rounds else ""
        )
        await git.commit_all(worktree, message)
        changed = await git.changed_files(worktree, base)
        if not changed:
            self._update(
                job_id, status="failed", error="Claude Code hat keine Änderungen vorgenommen"
            )
            self._log(job_id, "Keine Änderungen – nichts zu übernehmen.")
            return None
        config_before = await git.show_file(worktree, base, "config.yaml")
        config_file = worktree / "config.yaml"
        config_after = config_file.read_text(encoding="utf-8") if config_file.is_file() else None
        problems = guard.violations(changed, config_before=config_before, config_after=config_after)
        self._update(job_id, changed_files=json.dumps(changed))
        if problems:
            self._update(
                job_id, status="failed", error="Schutzregeln verletzt: " + "; ".join(problems)
            )
            self._log(
                job_id, "⛔ Schutzregeln verletzt – Änderung verworfen: " + "; ".join(problems)
            )
            self.app.notify(
                "Selbstverbesserung blockiert", f"#{job_id} wollte geschützte Bereiche ändern."
            )
            return None
        self._log(job_id, f"Geänderte Dateien: {', '.join(changed)}")
        return changed

    async def _test(
        self, job_id: int, worktree: Path, base: str, changed: list[str]
    ) -> tuple[bool, str]:
        python = base_python()
        if any(name in changed for name in REQUIREMENT_FILES):
            self._log(job_id, "Neue Abhängigkeiten – teste in der Sandbox-Umgebung.")
            python = await self.sandbox.ensure(worktree, lambda line: self._log(job_id, line))
        self._log(job_id, "Starte Tests (pytest) …")
        ok, output = await run_tests(worktree, python)
        summary = output.strip().splitlines()[-1] if output.strip() else "(keine Ausgabe)"
        self._log(job_id, ("✅ " if ok else "❌ ") + summary)
        return ok, output

    def _auto_merge_allowed(self, changed: list[str]) -> bool:
        mode = self.app.config.self_improve.auto_merge
        if mode == "always":
            return True
        if mode == "plugins_only":
            return bool(changed) and all(
                path.startswith(("jarvis/plugins/", "tests/")) for path in changed
            )
        return False

    # ------------------------------------------------------------------ Übernehmen

    async def approve(self, job_id: int) -> dict[str, Any]:
        job = self.get(job_id)
        if job is None or job["status"] != "ready":
            raise ValueError("Diese Verbesserung ist nicht zur Übernahme bereit")
        if not await git.is_clean(self.repo):
            raise ValueError(
                "Im JARVIS-Ordner gibt es eigene, nicht committete Änderungen. Bitte zuerst committen "
                "oder verwerfen, dann erneut übernehmen."
            )
        changed = await git.changed_files(self.repo, job["base_commit"], job["branch"])
        problems = guard.violations(
            changed,
            config_before=await git.show_file(self.repo, job["base_commit"], "config.yaml"),
            config_after=await git.show_file(self.repo, job["branch"], "config.yaml"),
        )
        if problems:
            self._update(
                job_id, status="failed", error="Schutzregeln verletzt: " + "; ".join(problems)
            )
            raise ValueError("Schutzregeln verletzt")
        self._update(job_id, status="merging")
        previous = await git.head(self.repo)
        try:
            await git.merge_no_ff(
                self.repo, job["branch"], f"JARVIS-Verbesserung #{job_id}: {job['request'][:60]}"
            )
        except git.GitError as exc:
            self._update(job_id, status="ready", error=f"Merge-Konflikt: {exc}")
            raise ValueError(f"Übernahme fehlgeschlagen (Konflikt): {exc}") from exc
        if any(name in changed for name in REQUIREMENT_FILES):
            self._log(job_id, "Installiere neue Abhängigkeiten …")
            from jarvis.self_improve.sandbox import _run, test_env

            code, output = await _run(
                [
                    base_python(),
                    "-m",
                    "pip",
                    "install",
                    "-q",
                    "-r",
                    str(self.repo / "requirements.txt"),
                ],
                cwd=self.repo,
                env=test_env(base_python()),
                limit_s=1800,
                log=lambda line: self._log(job_id, line),
            )
            if code != 0:
                await git.reset_hard(self.repo, previous)
                self._update(
                    job_id,
                    status="failed",
                    error="Paketinstallation fehlgeschlagen – Merge zurückgenommen",
                )
                raise ValueError(f"Paketinstallation fehlgeschlagen:\n{output[-1500:]}")
        self._write_changelog(job)
        await git.commit_paths(
            self.repo, f"CHANGELOG: JARVIS-Verbesserung #{job_id}", ["CHANGELOG.md"]
        )
        merged = await git.head(self.repo)
        marker = {
            "job_id": job_id,
            "previous_commit": previous,
            "merged_commit": merged,
            "db": str(self.db.path),
            "ts": now_iso(),
        }
        (self.app.data_dir / UPDATE_MARKER).write_text(json.dumps(marker), encoding="utf-8")
        await self._cleanup(job, delete_branch=False)
        self._update(job_id, status="merged", merge_commit=merged)
        self._log(job_id, "Übernommen. JARVIS startet neu …")
        self.app.notify("Verbesserung übernommen", f"#{job_id} ist aktiv – JARVIS startet neu.")
        if self._restart is not None:
            asyncio.get_running_loop().call_later(2, self._restart)
        result = self.get(job_id)
        assert result is not None
        return result

    def _write_changelog(self, job: dict[str, Any]) -> None:
        path = self.repo / "CHANGELOG.md"
        text = path.read_text(encoding="utf-8") if path.is_file() else "# Changelog\n"
        summary = (job.get("summary") or "").strip().splitlines()
        first = next((line.strip("#*- ").strip() for line in summary if line.strip()), "")
        first = re.sub(r"\*\*|__|`", "", first)
        entry = (
            f"- {datetime.now():%Y-%m-%d} · #{job['id']} · {_shorten(job['request'], 120)}"
            + (f"\n  {_shorten(first, 240)}" if first else "")
            + f"\n  (Branch `{job['branch']}`, Tests bestanden)\n"
        )
        heading = "## Selbstverbesserungen"
        if heading in text:
            index = text.index(heading) + len(heading)
            text = text[:index] + "\n\n" + entry + text[index:].lstrip("\n")
        else:
            lines = text.split("\n## ", 1)
            text = (
                lines[0].rstrip()
                + f"\n\n{heading}\n\n{entry}"
                + (f"\n## {lines[1]}" if len(lines) > 1 else "")
            )
        path.write_text(text, encoding="utf-8")

    # ------------------------------------------------------------------ Selbst-Check

    async def self_check(self) -> list[dict[str, Any]]:
        """Sucht selbstständig nach Verbesserungen und legt Vorschläge an (ohne sie umzusetzen)."""
        if self.unavailable_reason():
            return []
        problems = self._collect_problems()
        run = ClaudeCodeRun(
            prompt=SELF_CHECK_PROMPT.format(problems=problems or "(keine Fehler protokolliert)"),
            cwd=self.repo,
            model=self.app.config.self_improve.claude_model,
            append_system_prompt="Du analysierst nur und änderst nichts.",
            tools=["Read", "Grep", "Glob"],
            permission_mode="plan",
            max_turns=30,
            timeout_s=1200,
        )
        text = ""
        try:
            async for event in self._runner(run):
                if event.kind == "result" and event.result is not None:
                    if event.result.is_error:
                        log.info("Selbst-Check: %s", event.result.text)
                        return []
                    text = event.result.text
        except ClaudeCodeError as exc:
            log.info("Selbst-Check nicht möglich: %s", exc)
            return []
        try:
            items = extract_json(text)
        except ValueError:
            return []
        created = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or not item.get("title"):
                continue
            suggestion_id = self.db.execute(
                "INSERT INTO improvement_suggestions (title, description, created) VALUES (?, ?, ?)",
                (str(item["title"])[:120], str(item.get("description", ""))[:2000], now_iso()),
            )
            created.append({"id": suggestion_id, "title": item["title"]})
        if created:
            self.app.events.publish("improvement.suggestions", {"count": len(created)})
            self.app.notify(
                "Verbesserungsvorschläge",
                f"Ich habe {len(created)} Ideen, wie ich besser werden kann – siehe Dashboard.",
            )
        return created

    def _collect_problems(self) -> str:
        lines: list[str] = []
        log_dir = self.app.data_dir / "logs"
        for path in sorted(log_dir.glob("jarvis-*.log")):
            try:
                content = path.read_text(encoding="utf-8", errors="replace").splitlines()[-3000:]
            except OSError:
                continue
            lines += [line[:300] for line in content if " ERROR " in line or "Traceback" in line][
                -15:
            ]
        # Werkzeug-Fehler der letzten 7 Tage (ab Tagesbeginn; ISO-Zeitstempel sind sortierbar).
        since = (datetime.now().astimezone() - timedelta(days=PROBLEM_LOOKBACK_DAYS)).strftime(
            "%Y-%m-%d"
        )
        for entry in self.app.audit.failures_since(since, limit=15):
            lines.append(
                f"Werkzeug {entry['tool']} fehlgeschlagen: {str(entry.get('result'))[:200]}"
            )
        return "\n".join(lines[-40:])


def _shorten(text: str, limit: int) -> str:
    """Kürzt an einer Wortgrenze und hängt „…“ an."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:–- ")
    return f"{cut} …"


def _describe_denial(denial: Any) -> str:
    """Abgelehnter Werkzeugaufruf von Claude Code in lesbarer Form."""
    if not isinstance(denial, Mapping):
        return str(denial)[:200]
    name = str(denial.get("tool_name") or "?")
    details = denial.get("tool_input")
    return f"{name}: {_describe_tool(details) if isinstance(details, Mapping) else ''}".rstrip(": ")


def _describe_tool(data: Mapping[str, Any]) -> str:
    for key in ("file_path", "path", "command", "pattern", "url", "query"):
        if key in data:
            return str(data[key])[:200]
    return json.dumps(data, ensure_ascii=False)[:200]


def read_update_marker(data_dir: Path) -> dict[str, Any] | None:
    path = data_dir / UPDATE_MARKER
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
