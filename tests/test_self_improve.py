"""Selbstverbesserung: Worktree, Schutzregeln, Tests, Freigabe, Rollback – mit Fake-Claude-Code.

Alles läuft in einem temporären Git-Repository mit einem winzigen Python-Paket.
Claude Code wird durch :class:`FakeClaude` ersetzt, das Dateien im Worktree
ändert und typische Ereignisse liefert. Die Tests im Worktree laufen echt (pytest).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from jarvis.core import supervisor
from jarvis.core.app import JarvisApp
from jarvis.core.db import Database
from jarvis.integrations import claude_code
from jarvis.integrations.claude_code import CCEvent, CCResult, ClaudeCodeError, ClaudeCodeRun
from jarvis.security.audit import AuditEntry
from jarvis.self_improve import git
from jarvis.self_improve import sandbox as sandbox_module
from jarvis.self_improve.manager import (
    UPDATE_MARKER,
    ImprovementManager,
    read_update_marker,
)
from jarvis.self_improve.sandbox import Sandbox, requirements_hash, venv_python

IDENTITY = ("-c", "user.name=Test", "-c", "user.email=test@localhost")
REQUEST = "Füge eine Funktion add3 hinzu"
SUMMARY = "## Neue Funktion add3\nAddiert drei Zahlen, mit Tests."

Edit = Callable[[Path], None]


def sh_git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *IDENTITY, *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def write(rel: str, text: str) -> Edit:
    def edit(root: Path) -> None:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    return edit


def edits(*items: Edit) -> Edit:
    def edit(root: Path) -> None:
        for item in items:
            item(root)

    return edit


ADD3_TEST = write(
    "tests/test_extra.py",
    "from mypkg.extra import add3\n\n\ndef test_add3() -> None:\n    assert add3(1, 2, 3) == 6\n",
)
GOOD_ADD3 = write(
    "mypkg/extra.py", "def add3(a: int, b: int, c: int) -> int:\n    return a + b + c\n"
)
BAD_ADD3 = write(
    "mypkg/extra.py", "def add3(a: int, b: int, c: int) -> int:\n    return a - b - c\n"
)
FEATURE = edits(GOOD_ADD3, ADD3_TEST)
BROKEN_FEATURE = edits(BAD_ADD3, ADD3_TEST)


class FakeClaude:
    """Spielt Claude Code: je Lauf eine Änderung im Worktree plus typische Ereignisse."""

    def __init__(
        self, *rounds: Edit, summary: str = SUMMARY, error: str | None = None, block: bool = False
    ) -> None:
        self.rounds = list(rounds)
        self.summary = summary
        self.error = error
        self.block = block
        self.runs: list[ClaudeCodeRun] = []
        self.started = asyncio.Event()

    async def __call__(self, run: ClaudeCodeRun) -> AsyncGenerator[CCEvent, None]:
        self.runs.append(run)
        yield CCEvent("init", raw={"session_id": "sitzung-1", "model": "sonnet"})
        self.started.set()
        if self.block:
            await asyncio.sleep(30)
        yield CCEvent("text", text="Ich lese den Code.\nDann ")
        yield CCEvent("tool_use", tool_name="Edit", tool_input={"file_path": "mypkg/extra.py"})
        if self.rounds:
            self.rounds.pop(0)(run.cwd)
        yield CCEvent("status", text="Claude: neuer Versuch 1/3")
        yield CCEvent("text", text="ändere ich ihn.")
        if self.error is not None:
            result = CCResult(is_error=True, text=self.error, error_kind="rate_limit")
        else:
            result = CCResult(
                is_error=False,
                text=self.summary,
                session_id="sitzung-1",
                permission_denials=["Edit(jarvis/security/guard.py)"],
            )
        yield CCEvent("result", text=result.text, result=result)


class Restart:
    def __init__(self) -> None:
        self.calls = 0
        self.delays: list[float] = []

    def __call__(self) -> None:
        self.calls += 1


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Winziges Git-Repository: Paket + Tests + config.yaml + CHANGELOG."""
    root = tmp_path / "repo"
    write("mypkg/__init__.py", "def add(a: int, b: int) -> int:\n    return a + b\n")(root)
    write(
        "tests/test_mypkg.py",
        "from mypkg import add\n\n\ndef test_add() -> None:\n    assert add(2, 3) == 5\n",
    )(root)
    write("pytest.ini", "[pytest]\ntestpaths = tests\n")(root)
    write(".gitignore", "__pycache__/\n*.pyc\n.pytest_cache/\n")(root)
    write("config.yaml", "assistant:\n  name: JARVIS\nsecurity:\n  confirm_timeout_s: 600\n")(root)
    write("CHANGELOG.md", "# Changelog\n\n## 1.0.0\n\n- Erste Version\n")(root)
    sh_git(root, "init", "-q", "-b", "main")
    sh_git(root, "add", "-A")
    sh_git(root, "commit", "-q", "-m", "Start")
    return root


@pytest.fixture
async def app(config_file: Path) -> AsyncIterator[JarvisApp]:
    application = JarvisApp.bootstrap(config_file, role="test", detect=False)
    yield application
    await application.aclose()


@pytest.fixture
async def restart(monkeypatch: pytest.MonkeyPatch) -> Restart:
    """Neustart-Callback; ``call_later`` führt ihn sofort aus statt nach 2 Sekunden."""
    recorder = Restart()
    loop = asyncio.get_running_loop()
    original = loop.call_later

    def call_later(delay: float, callback: Callable[..., Any], *args: Any) -> asyncio.TimerHandle:
        if callback is recorder:
            recorder.delays.append(delay)
            delay = 0
        return original(delay, callback, *args)

    monkeypatch.setattr(loop, "call_later", call_later)
    return recorder


def make_manager(app: JarvisApp, repo: Path, runner: FakeClaude, restart: Restart | None = None):  # type: ignore[no-untyped-def]
    return ImprovementManager(app, repo=repo, runner=runner, restart=restart)


async def ready_job(manager: ImprovementManager, request: str = REQUEST) -> dict[str, Any]:
    job = manager.request(request)
    await manager.process(job)
    result = manager.get(job["id"])
    assert result is not None
    assert result["status"] == "ready", result.get("error")
    return result


# ---------------------------------------------------------------------- Ablauf bis "bereit"


async def test_job_runs_in_worktree_and_becomes_ready(app: JarvisApp, repo: Path) -> None:
    fake = FakeClaude(FEATURE)
    manager = make_manager(app, repo, fake)
    base = sh_git(repo, "rev-parse", "HEAD")

    job = manager.request(REQUEST)
    assert job["status"] == "queued" and job["source"] == "user"
    await manager.process(job)

    result = manager.get(job["id"])
    assert result is not None
    assert result["status"] == "ready"
    assert result["branch"].startswith(f"jarvis/verbesserung-{job['id']}-fuege-eine-funktion")
    worktree = Path(result["worktree"])
    assert worktree == app.data_dir / "worktrees" / str(job["id"])
    assert (worktree / "mypkg" / "extra.py").is_file()
    assert result["base_commit"] == base
    assert result["result_commit"] == sh_git(repo, "rev-parse", result["branch"])
    assert result["changed_files"] == ["mypkg/extra.py", "tests/test_extra.py"]
    assert "mypkg/extra.py" in result["diff_stat"]
    assert "passed" in result["test_output"]
    assert result["summary"] == SUMMARY
    assert result["session_id"] == "sitzung-1"

    # Der laufende Code bleibt unberührt.
    assert sh_git(repo, "rev-parse", "HEAD") == base
    assert not (repo / "mypkg" / "extra.py").exists()
    assert "+def add3" in await manager.diff(job["id"])

    run = fake.runs[0]
    assert run.cwd == worktree
    assert REQUEST in run.prompt
    assert run.permission_mode == "acceptEdits"
    assert run.resume_session is None and run.keep_session
    assert "Edit(jarvis/security/**)" in run.disallowed_tools
    assert "Write(jarvis/core/supervisor.py)" in run.disallowed_tools

    lines = "\n".join(manager.read_log(job["id"]))
    for expected in (
        f"Auftrag: {REQUEST}",
        "💬 Ich lese den Code.",
        "💬 Dann ändere ich ihn.",
        "🔧 Edit: mypkg/extra.py",
        "Claude: neuer Versuch 1/3",
        "⛔ Nicht erlaubt: Edit(jarvis/security/guard.py)",
        "Geänderte Dateien: mypkg/extra.py, tests/test_extra.py",
        "✅",
    ):
        assert expected in lines
    assert manager.list_jobs()[0]["id"] == job["id"]
    assert any(e.data.get("title") == "Verbesserung bereit" for e in app.events.recent(100))


@pytest.mark.parametrize(
    "path",
    [
        "jarvis/security/guard.py",
        "jarvis/self_improve/manager.py",
        "jarvis/core/supervisor.py",
        "config.local.yaml",
    ],
)
async def test_guard_rejects_protected_paths(app: JarvisApp, repo: Path, path: str) -> None:
    fake = FakeClaude(edits(FEATURE, write(path, "# manipuliert\n")))
    manager = make_manager(app, repo, fake)
    job = manager.request("Mach die Sicherheit lockerer")
    await manager.process(job)

    result = manager.get(job["id"])
    assert result is not None
    assert result["status"] == "failed"
    assert result["error"] == f"Schutzregeln verletzt: geschützte Datei geändert: {path}"
    assert result["test_output"] is None  # gar nicht erst getestet
    assert path in result["changed_files"]


async def test_guard_rejects_protected_config_sections(app: JarvisApp, repo: Path) -> None:
    config = "assistant:\n  name: JARVIS\nsecurity:\n  confirm_timeout_s: 1\n"
    manager = make_manager(app, repo, FakeClaude(write("config.yaml", config)))
    job = manager.request("Weniger Rückfragen")
    await manager.process(job)

    result = manager.get(job["id"])
    assert result is not None and result["status"] == "failed"
    assert "geschützter Konfigurationsabschnitt geändert: security" in result["error"]


async def test_unprotected_config_change_is_allowed(app: JarvisApp, repo: Path) -> None:
    config = "assistant:\n  name: FRIDAY\nsecurity:\n  confirm_timeout_s: 600\n"
    manager = make_manager(app, repo, FakeClaude(write("config.yaml", config)))
    result = await ready_job(manager, "Nenn dich FRIDAY")
    assert result["changed_files"] == ["config.yaml"]


async def test_no_changes_marks_job_failed(app: JarvisApp, repo: Path) -> None:
    manager = make_manager(app, repo, FakeClaude())
    job = manager.request("Tu nichts")
    await manager.process(job)
    result = manager.get(job["id"])
    assert result is not None and result["status"] == "failed"
    assert result["error"] == "Claude Code hat keine Änderungen vorgenommen"


async def test_failing_tests_trigger_fix_round(app: JarvisApp, repo: Path) -> None:
    fake = FakeClaude(BROKEN_FEATURE, GOOD_ADD3)
    manager = make_manager(app, repo, fake)
    result = await ready_job(manager)

    assert len(fake.runs) == 2
    fix = fake.runs[1]
    assert fix.resume_session == "sitzung-1"
    assert fix.prompt.startswith("Die Tests schlagen fehl")
    assert "test_add3" in fix.prompt  # Testausgabe wird mitgeschickt
    log = sh_git(repo, "log", "--format=%s", f"{result['base_commit']}..{result['branch']}")
    assert log.splitlines() == [
        f"JARVIS-Verbesserung #{result['id']} (Nachbesserung 1)",
        f"JARVIS-Verbesserung #{result['id']}",
    ]
    assert "Runde 1" in "\n".join(manager.read_log(result["id"]))


async def test_tests_still_failing_after_fix_rounds(app: JarvisApp, repo: Path) -> None:
    app.config.self_improve.max_fix_rounds = 1
    fake = FakeClaude(BROKEN_FEATURE, write("mypkg/extra.py", "def add3(a, b, c):\n    return 0\n"))
    manager = make_manager(app, repo, fake)
    job = manager.request(REQUEST)
    await manager.process(job)

    result = manager.get(job["id"])
    assert result is not None and result["status"] == "failed"
    assert result["error"] == "Tests schlagen fehl – siehe Testausgabe"
    assert "failed" in result["test_output"]
    assert len(fake.runs) == 2
    assert "❌" in "\n".join(manager.read_log(job["id"]))


async def test_claude_error_result_raises(app: JarvisApp, repo: Path) -> None:
    manager = make_manager(app, repo, FakeClaude(FEATURE, error="Usage limit reached"))
    job = manager.request(REQUEST)
    with pytest.raises(ClaudeCodeError) as info:
        await manager.process(job)
    assert info.value.kind == "rate_limit"


async def test_changed_requirements_are_tested_in_sandbox(
    app: JarvisApp, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeClaude(edits(FEATURE, write("requirements.txt", "requests\n")))
    manager = make_manager(app, repo, fake)
    worktrees: list[Path] = []

    async def ensure(worktree: Path, log: Callable[[str], None]) -> str:
        worktrees.append(worktree)
        return sys.executable

    monkeypatch.setattr(manager.sandbox, "ensure", ensure)
    result = await ready_job(manager)
    assert worktrees == [Path(result["worktree"])]
    assert "Sandbox-Umgebung" in "\n".join(manager.read_log(result["id"]))


# ---------------------------------------------------------------------- Freigabe


async def test_approve_merges_writes_changelog_marker_and_restarts(
    app: JarvisApp, repo: Path, restart: Restart
) -> None:
    manager = make_manager(app, repo, FakeClaude(FEATURE), restart)
    job = await ready_job(manager)
    base = job["base_commit"]
    (repo / "notizen.txt").write_text("eigene, unversionierte Datei", encoding="utf-8")

    result = await manager.approve(job["id"])

    head = sh_git(repo, "rev-parse", "HEAD")
    assert result["status"] == "merged"
    assert result["merge_commit"] == head
    assert (repo / "mypkg" / "extra.py").is_file()
    # HEAD = CHANGELOG-Commit, davor der Merge-Commit mit zwei Eltern.
    assert (
        sh_git(repo, "log", "-1", "--format=%s", "HEAD")
        == f"CHANGELOG: JARVIS-Verbesserung #{job['id']}"
    )
    assert sh_git(repo, "log", "-1", "--format=%s", "HEAD~1") == (
        f"JARVIS-Verbesserung #{job['id']}: {REQUEST}"
    )
    assert len(sh_git(repo, "rev-list", "--parents", "-n", "1", "HEAD~1").split()) == 3
    assert sh_git(repo, "show", "--format=", "--name-only", "HEAD") == "CHANGELOG.md"
    # Fremde, unversionierte Dateien bleiben unversioniert (und überleben so einen Rollback).
    assert sh_git(repo, "status", "--porcelain") == "?? notizen.txt"

    changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert changelog.startswith("# Changelog\n\n## Selbstverbesserungen\n\n- ")
    assert f"#{job['id']} · {REQUEST}\n  Neue Funktion add3\n" in changelog
    assert f"(Branch `{job['branch']}`, Tests bestanden)" in changelog
    assert changelog.index("## Selbstverbesserungen") < changelog.index("## 1.0.0")

    marker = read_update_marker(app.data_dir)
    assert marker is not None
    assert marker["job_id"] == job["id"]
    assert marker["previous_commit"] == base
    assert marker["merged_commit"] == head
    assert marker["db"] == str(app.db.path)

    assert not Path(job["worktree"]).exists()  # Worktree aufgeräumt …
    assert sh_git(repo, "branch", "--list", job["branch"])  # … Branch bleibt erhalten
    await asyncio.sleep(0.01)
    assert restart.calls == 1 and restart.delays == [2]


async def test_approve_requires_ready_job_and_clean_repo(app: JarvisApp, repo: Path) -> None:
    manager = make_manager(app, repo, FakeClaude(FEATURE))
    queued = manager.request("Später")
    with pytest.raises(ValueError, match="nicht zur Übernahme bereit"):
        await manager.approve(queued["id"])
    with pytest.raises(ValueError, match="nicht zur Übernahme bereit"):
        await manager.approve(999)

    job = await ready_job(manager)
    (repo / "mypkg" / "__init__.py").write_text("# lokale Änderung\n", encoding="utf-8")
    with pytest.raises(ValueError, match="nicht committete Änderungen"):
        await manager.approve(job["id"])
    assert (manager.get(job["id"]) or {})["status"] == "ready"


async def test_approve_rechecks_guard_on_branch(app: JarvisApp, repo: Path) -> None:
    manager = make_manager(app, repo, FakeClaude(FEATURE))
    job = await ready_job(manager)
    # Nachträglich (z. B. von Hand) eine geschützte Datei auf den Branch geschmuggelt.
    worktree = Path(job["worktree"])
    write("jarvis/security/guard.py", "ALLES_ERLAUBT = True\n")(worktree)
    sh_git(worktree, "add", "-A")
    sh_git(worktree, "commit", "-q", "-m", "heimlich")
    head = sh_git(repo, "rev-parse", "HEAD")

    with pytest.raises(ValueError, match="Schutzregeln verletzt"):
        await manager.approve(job["id"])
    result = manager.get(job["id"])
    assert result is not None and result["status"] == "failed"
    assert sh_git(repo, "rev-parse", "HEAD") == head  # nichts übernommen


async def test_approve_with_merge_conflict_keeps_job_ready(app: JarvisApp, repo: Path) -> None:
    manager = make_manager(app, repo, FakeClaude(FEATURE))
    job = await ready_job(manager)
    write("mypkg/extra.py", "KONFLIKT = 1\n")(repo)
    sh_git(repo, "add", "-A")
    sh_git(repo, "commit", "-q", "-m", "Parallele Änderung")

    with pytest.raises(ValueError, match="Konflikt"):
        await manager.approve(job["id"])
    result = manager.get(job["id"])
    assert result is not None and result["status"] == "ready"
    assert "Merge-Konflikt" in result["error"]
    assert await git.is_clean(repo)  # Merge abgebrochen
    assert not (app.data_dir / UPDATE_MARKER).exists()


async def test_failed_package_install_undoes_merge(
    app: JarvisApp, repo: Path, restart: Restart, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeClaude(edits(FEATURE, write("requirements.txt", "requests\n")))
    manager = make_manager(app, repo, fake, restart)

    async def ensure(worktree: Path, log: Callable[[str], None]) -> str:
        return sys.executable

    monkeypatch.setattr(manager.sandbox, "ensure", ensure)
    job = await ready_job(manager)
    pip_calls: list[list[str]] = []

    async def fake_run(args: list[str], **kwargs: Any) -> tuple[int, str]:
        pip_calls.append(args)
        return 1, "ERROR: kein Netz"

    monkeypatch.setattr(sandbox_module, "_run", fake_run)
    with pytest.raises(ValueError, match="Paketinstallation fehlgeschlagen"):
        await manager.approve(job["id"])

    assert pip_calls and pip_calls[0][1:5] == ["-m", "pip", "install", "-q"]
    assert pip_calls[0][-1] == str(repo / "requirements.txt")
    assert sh_git(repo, "rev-parse", "HEAD") == job["base_commit"]
    result = manager.get(job["id"])
    assert result is not None and result["status"] == "failed"
    assert "Merge zurückgenommen" in result["error"]
    assert restart.calls == 0


async def test_auto_merge_plugins_only(app: JarvisApp, repo: Path, restart: Restart) -> None:
    app.config.self_improve.auto_merge = "plugins_only"
    plugin = edits(
        write("jarvis/plugins/hello.py", "def hello() -> str:\n    return 'Hallo'\n"),
        write("jarvis/__init__.py", ""),
        write("jarvis/plugins/__init__.py", ""),
    )
    manager = make_manager(app, repo, FakeClaude(plugin), restart)
    job = manager.request("Begrüßungs-Plugin")
    await manager.process(job)
    # jarvis/__init__.py liegt außerhalb von jarvis/plugins/ → keine automatische Übernahme.
    assert (manager.get(job["id"]) or {})["status"] == "ready"

    assert manager._auto_merge_allowed(["jarvis/plugins/a.py", "tests/test_a.py"])
    assert not manager._auto_merge_allowed([])
    app.config.self_improve.auto_merge = "never"
    assert not manager._auto_merge_allowed(["jarvis/plugins/a.py"])
    app.config.self_improve.auto_merge = "always"
    assert manager._auto_merge_allowed(["jarvis/core/app.py"])

    app.config.self_improve.auto_merge = "plugins_only"
    only_plugin = make_manager(
        app, repo, FakeClaude(write("jarvis/plugins/hello.py", "X = 1\n")), restart
    )
    second = only_plugin.request("Nur ein Plugin")
    await only_plugin.process(second)
    assert (only_plugin.get(second["id"]) or {})["status"] == "merged"
    await asyncio.sleep(0.01)
    assert restart.calls == 1


def test_changelog_entries_are_added_newest_first(app: JarvisApp, repo: Path) -> None:
    manager = make_manager(app, repo, FakeClaude())
    (repo / "CHANGELOG.md").unlink()
    manager._write_changelog({"id": 1, "request": "Erste", "summary": "", "branch": "b1"})
    manager._write_changelog(
        {"id": 2, "request": "Zweite", "summary": "\n- **Kurz:** gut\n", "branch": "b2"}
    )
    text = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert text.startswith("# Changelog\n\n## Selbstverbesserungen\n\n")
    assert text.index("#2 · Zweite\n  Kurz: gut\n  (Branch `b2`") < text.index(
        "#1 · Erste\n  (Branch `b1`"
    )


# ---------------------------------------------------------------------- Verwerfen & Abbrechen


async def test_reject_removes_worktree_and_branch(app: JarvisApp, repo: Path) -> None:
    manager = make_manager(app, repo, FakeClaude(FEATURE))
    job = await ready_job(manager)
    assert await manager.reject(job["id"]) is True

    result = manager.get(job["id"])
    assert result is not None and result["status"] == "rejected"
    assert not Path(job["worktree"]).exists()
    assert sh_git(repo, "branch", "--list", job["branch"]) == ""
    assert await manager.reject(job["id"]) is False  # nur ready/failed
    assert await manager.reject(999) is False


async def test_cancel_queued_job(app: JarvisApp, repo: Path) -> None:
    manager = make_manager(app, repo, FakeClaude())
    job = manager.request("Irgendwann")
    assert await manager.cancel(job["id"]) is True
    assert (manager.get(job["id"]) or {})["status"] == "cancelled"
    assert await manager.cancel(job["id"]) is False
    assert await manager.cancel(999) is False


async def test_worker_runs_job_and_cancel_cleans_up(
    app: JarvisApp, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeClaude(FEATURE, block=True)
    manager = make_manager(app, repo, fake)
    monkeypatch.setattr(manager, "unavailable_reason", lambda: None)
    manager.start()
    try:
        job = manager.request(REQUEST)
        await asyncio.wait_for(fake.started.wait(), 5)
        running = manager.get(job["id"])
        assert running is not None and running["status"] == "running"
        assert Path(running["worktree"]).is_dir()

        assert await manager.cancel(job["id"]) is True
        for _ in range(200):
            if (manager.get(job["id"]) or {}).get("status") == "cancelled":
                break
            await asyncio.sleep(0.01)
        result = manager.get(job["id"])
        assert result is not None and result["status"] == "cancelled"
        assert not Path(running["worktree"]).exists()
        assert sh_git(repo, "branch", "--list", running["branch"]) == ""
        assert "Abgebrochen." in "\n".join(manager.read_log(job["id"]))
    finally:
        await manager.stop()


async def test_worker_marks_crashed_job_failed(
    app: JarvisApp, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = make_manager(app, repo, FakeClaude(FEATURE, error="Claude ist kaputt"))
    monkeypatch.setattr(manager, "unavailable_reason", lambda: None)
    manager.start()
    try:
        job = manager.request(REQUEST)
        for _ in range(300):
            if (manager.get(job["id"]) or {}).get("status") == "failed":
                break
            await asyncio.sleep(0.01)
        result = manager.get(job["id"])
        assert result is not None and result["status"] == "failed"
        assert result["error"] == "Claude ist kaputt"
    finally:
        await manager.stop()


async def test_start_requeues_interrupted_jobs(
    app: JarvisApp, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = make_manager(app, repo, FakeClaude())
    monkeypatch.setattr(manager, "unavailable_reason", lambda: "aus")  # Worker bleibt untätig
    job = manager.request("Unterbrochen")
    manager._update(job["id"], status="testing")
    manager.start()
    try:
        assert (manager.get(job["id"]) or {})["status"] == "queued"
    finally:
        await manager.stop()


# ---------------------------------------------------------------------- Status & Selbst-Check


async def test_unavailable_reason(
    app: JarvisApp, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        claude_code, "find_claude_executable", lambda configured=None: "/bin/claude"
    )
    manager = make_manager(app, repo, FakeClaude())
    assert await manager.check_repo() is True
    assert manager.unavailable_reason() is None

    app.config.self_improve.enabled = False
    assert manager.unavailable_reason() == "Selbstverbesserung ist ausgeschaltet"
    app.config.self_improve.enabled = True

    missing = make_manager(app, repo, FakeClaude())
    monkeypatch.setattr(claude_code, "find_claude_executable", lambda configured=None: None)
    assert "Claude Code nicht installiert" in (missing.unavailable_reason() or "")

    no_repo = make_manager(app, tmp_path / "kein-repo", FakeClaude())
    (tmp_path / "kein-repo").mkdir()
    assert await no_repo.check_repo() is False
    assert "kein Git-Repository" in (no_repo.unavailable_reason() or "")

    monkeypatch.setattr(git, "git_executable", lambda: None)
    assert "Git fehlt" in (manager.unavailable_reason() or "")


async def test_self_check_creates_suggestions(
    app: JarvisApp, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs: list[ClaudeCodeRun] = []
    answer = json.dumps(
        [
            {"title": "Timeout erhöhen", "description": "web_fetch braucht länger"},
            {"description": "ohne Titel wird ignoriert"},
        ]
    )

    async def runner(run: ClaudeCodeRun) -> AsyncGenerator[CCEvent, None]:
        runs.append(run)
        yield CCEvent("result", result=CCResult(is_error=False, text=f"Vorschläge:\n{answer}"))

    manager = ImprovementManager(app, repo=repo, runner=runner)
    monkeypatch.setattr(manager, "unavailable_reason", lambda: None)
    app.audit.record(
        AuditEntry(
            source="chat",
            tool="web_fetch",
            arguments={},
            level="auto",
            decision="auto",
            ok=False,
            result="Zeitüberschreitung",
        )
    )

    created = await manager.self_check()

    assert [c["title"] for c in created] == ["Timeout erhöhen"]
    assert runs[0].cwd == repo
    assert runs[0].permission_mode == "plan"
    assert list(runs[0].tools or []) == ["Read", "Grep", "Glob"]
    assert "Werkzeug web_fetch fehlgeschlagen: Zeitüberschreitung" in runs[0].prompt
    suggestion = manager.suggestions()[0]
    assert suggestion["description"] == "web_fetch braucht länger"

    job = manager.implement_suggestion(suggestion["id"])
    assert job is not None
    assert job["source"] == "self_check" and job["status"] == "queued"
    assert job["request"] == "Timeout erhöhen: web_fetch braucht länger"
    assert manager.suggestions() == []
    assert manager.implement_suggestion(999) is None


async def test_self_check_handles_errors(
    app: JarvisApp, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing(run: ClaudeCodeRun) -> AsyncGenerator[CCEvent, None]:
        raise ClaudeCodeError("nicht installiert", kind="not_installed")
        yield CCEvent("other")  # pragma: no cover - macht die Funktion zum Generator

    async def error_result(run: ClaudeCodeRun) -> AsyncGenerator[CCEvent, None]:
        yield CCEvent("result", result=CCResult(is_error=True, text="Limit erreicht"))

    async def no_json(run: ClaudeCodeRun) -> AsyncGenerator[CCEvent, None]:
        yield CCEvent("result", result=CCResult(is_error=False, text="Alles prima."))

    for runner in (failing, error_result, no_json):
        manager = ImprovementManager(app, repo=repo, runner=runner)
        monkeypatch.setattr(manager, "unavailable_reason", lambda: None)
        assert await manager.self_check() == []

    unavailable = ImprovementManager(app, repo=repo, runner=no_json)
    monkeypatch.setattr(unavailable, "unavailable_reason", lambda: "aus")
    assert await unavailable.self_check() == []


def test_dismiss_suggestion(app: JarvisApp, repo: Path) -> None:
    manager = make_manager(app, repo, FakeClaude())
    suggestion_id = app.db.execute(
        "INSERT INTO improvement_suggestions (title, description, created) VALUES ('t', 'd', 'x')"
    )
    manager.dismiss_suggestion(suggestion_id)
    assert manager.suggestions() == []


def test_collect_problems_looks_back_seven_days(app: JarvisApp, repo: Path) -> None:
    manager = make_manager(app, repo, FakeClaude())
    log_dir = app.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "jarvis-dienst.log").write_text(
        "2026-10-12 10:00:00 INFO alles gut\n2026-10-12 10:00:01 ERROR jarvis.web: kaputt\n",
        encoding="utf-8",
    )
    for tool, days_ago, ok in (
        ("heute", 0, False),
        ("vor_drei_tagen", 3, False),
        ("vor_zehn_tagen", 10, False),
        ("erfolgreich", 1, True),
    ):
        entry_id = app.audit.record(
            AuditEntry(
                source="chat",
                tool=tool,
                arguments={},
                level="auto",
                decision="auto",
                ok=ok,
                result=f"Ergebnis {tool}",
            )
        )
        app.db.execute(
            "UPDATE audit_log SET ts = datetime('now', ?, 'localtime') WHERE id = ?",
            (f"-{days_ago} days", entry_id),
        )

    problems = manager._collect_problems()

    assert "ERROR jarvis.web: kaputt" in problems
    assert "alles gut" not in problems
    assert "Werkzeug heute fehlgeschlagen" in problems
    assert "Werkzeug vor_drei_tagen fehlgeschlagen" in problems
    assert "vor_zehn_tagen" not in problems
    assert "erfolgreich" not in problems


# ---------------------------------------------------------------------- Git & Sandbox


async def test_git_helpers(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "leer").mkdir()
    assert await git.is_repo(repo) is True
    assert await git.is_repo(tmp_path / "leer") is False
    assert await git.current_branch(repo) == "main"
    assert await git.is_clean(repo)
    assert await git.commit_all(repo, "nichts") is False
    assert await git.show_file(repo, "HEAD", "fehlt.txt") is None
    assert await git.show_file(repo, "HEAD", "pytest.ini") == "[pytest]\ntestpaths = tests\n"

    (repo / "neu.txt").write_text("x", encoding="utf-8")
    assert await git.is_clean(repo)  # unversionierte Dateien zählen nicht
    (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    assert not await git.is_clean(repo)
    base = await git.head(repo)
    assert await git.commit_all(repo, "Änderung") is True
    assert await git.changed_files(repo, base) == ["neu.txt", "pytest.ini"]
    assert "2 files changed" in await git.diff_stat(repo, base)
    await git.reset_hard(repo, base)
    assert await git.head(repo) == base

    with pytest.raises(git.GitError, match="fehlgeschlagen"):
        await git.run_git("rev-parse", "gibt-es-nicht", cwd=repo)
    monkeypatch.setattr(git, "git_executable", lambda: None)
    with pytest.raises(git.GitError, match="nicht installiert"):
        await git.head(repo)
    assert await git.is_repo(repo) is False


async def test_merge_conflict_is_aborted(repo: Path, tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    base = await git.head(repo)
    await git.worktree_add(repo, worktree, "feature", base)
    write("pytest.ini", "[pytest]\n# Branch\n")(worktree)
    await git.commit_all(worktree, "Branch")
    write("pytest.ini", "[pytest]\n# Main\n")(repo)
    await git.commit_all(repo, "Main")

    with pytest.raises(git.GitError):
        await git.merge_no_ff(repo, "feature", "Merge")
    assert await git.is_clean(repo)
    await git.worktree_remove(repo, worktree)
    assert not worktree.exists()
    await git.delete_branch(repo, "feature")
    assert sh_git(repo, "branch", "--list", "feature") == ""


def test_requirements_hash_and_test_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = requirements_hash(tmp_path)
    (tmp_path / "requirements.txt").write_text("httpx\n", encoding="utf-8")
    first = requirements_hash(tmp_path)
    (tmp_path / "requirements-dev.txt").write_text("pytest\n", encoding="utf-8")
    assert len({empty, first, requirements_hash(tmp_path)}) == 3

    monkeypatch.setenv("JARVIS_CONFIG", "/irgendwo/config.yaml")
    env = sandbox_module.test_env(sys.executable)
    assert "JARVIS_CONFIG" not in env
    assert env["PYTHONUTF8"] == "1"
    assert env["JARVIS_DATA_DIR"].endswith("jarvis-test-data")
    assert env["PATH"].startswith(str(Path(sys.executable).parent))
    assert venv_python(Path("venv")).parent.name in ("bin", "Scripts")


async def test_run_tests_puts_worktree_first_on_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []

    async def fake_run(args: list[str], **kwargs: Any) -> tuple[int, str]:
        seen.append({"args": args, **kwargs})
        return 0, "1 passed"

    monkeypatch.setattr(sandbox_module, "_run", fake_run)
    monkeypatch.setenv("PYTHONPATH", "/anderswo")
    assert await sandbox_module.run_tests(tmp_path, sys.executable) == (True, "1 passed")
    call = seen[0]
    assert call["args"] == [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    assert call["cwd"] == tmp_path
    assert call["env"]["PYTHONPATH"] == f"{tmp_path}{os.pathsep}/anderswo"

    monkeypatch.delenv("PYTHONPATH")
    await sandbox_module.run_tests(tmp_path, sys.executable)
    assert seen[1]["env"]["PYTHONPATH"] == str(tmp_path)


WHERE_TEST = write(
    "tests/test_where.py",
    "import os\nfrom pathlib import Path\n\nimport jarvis\n\n\n"
    "def test_imports_worktree_code() -> None:\n"
    "    worktree = Path.cwd().resolve()\n"
    "    first = os.environ['PYTHONPATH'].split(os.pathsep)[0]\n"
    "    assert Path(first).resolve() == worktree\n"
    "    assert Path(jarvis.__file__).resolve().is_relative_to(worktree)\n"
    "    assert jarvis.HERKUNFT == 'worktree'\n",
)


async def test_worktree_tests_import_worktree_package(app: JarvisApp, repo: Path) -> None:
    """Echte Prüfung: pytest im Worktree importiert ``jarvis`` aus dem Worktree."""
    fake = FakeClaude(edits(write("jarvis/__init__.py", "HERKUNFT = 'worktree'\n"), WHERE_TEST))
    manager = make_manager(app, repo, fake)
    result = await ready_job(manager, "Herkunft prüfen")
    assert "2 passed" in result["test_output"]


async def test_sandbox_ensure_creates_venv_and_reinstalls_on_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    exit_code = 0

    async def fake_run(args: list[str], **kwargs: Any) -> tuple[int, str]:
        calls.append(args)
        if "venv" in args:
            python = venv_python(Path(args[-1]))
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")
        return exit_code, "Ausgabe"

    monkeypatch.setattr(sandbox_module, "_run", fake_run)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "requirements.txt").write_text("httpx\n", encoding="utf-8")
    box = Sandbox(tmp_path / "sandbox")
    lines: list[str] = []

    assert await box.ensure(worktree, lines.append) == str(box.python)
    assert [c[1:3] for c in calls] == [["-m", "venv"], ["-m", "pip"]]
    assert calls[1][-1] == str(worktree / "requirements.txt")
    assert (box.root / "requirements.sha256").read_text(encoding="utf-8") == requirements_hash(
        worktree
    )

    await box.ensure(worktree, lines.append)
    assert len(calls) == 2  # unveränderte Requirements → nichts zu tun

    (worktree / "requirements-dev.txt").write_text("pytest\n", encoding="utf-8")
    await box.ensure(worktree, lines.append)
    assert len(calls) == 3 and calls[2][-1] == str(worktree / "requirements-dev.txt")

    exit_code = 1
    (worktree / "requirements.txt").write_text("kaputt\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Paketinstallation"):
        await box.ensure(worktree, lines.append)
    assert any("Installiere Pakete" in line for line in lines)


# ---------------------------------------------------------------------- Supervisor


@pytest.fixture
def no_pip(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Fängt die pip-Aufrufe des Supervisors ab; Git läuft echt."""
    calls: list[list[str]] = []
    original = subprocess.run

    def run(args: Any, *a: Any, **kw: Any) -> Any:
        if isinstance(args, list) and "pip" in args:
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0)
        return original(args, *a, **kw)

    monkeypatch.setattr(supervisor.subprocess, "run", run)
    return calls


def test_supervisor_rollback_resets_to_previous_commit(
    repo: Path, tmp_path: Path, no_pip: list[list[str]]
) -> None:
    previous = sh_git(repo, "rev-parse", "HEAD")
    write("mypkg/extra.py", "KAPUTT = True\n")(repo)
    sh_git(repo, "add", "-A")
    sh_git(repo, "commit", "-q", "-m", "Update")
    merged = sh_git(repo, "rev-parse", "HEAD")

    data_dir = tmp_path / "daten"
    db = Database(data_dir / "jarvis.db")
    db.migrate()
    job_id = db.execute(
        "INSERT INTO improvements (request, status, created, updated) "
        "VALUES ('x', 'merged', 'jetzt', 'jetzt')"
    )
    db.close()
    marker = {"job_id": job_id, "previous_commit": previous, "merged_commit": merged,
              "db": str(data_dir / "jarvis.db")}  # fmt: skip
    (data_dir / supervisor.UPDATE_MARKER).write_text(json.dumps(marker), encoding="utf-8")
    assert supervisor._read_marker(data_dir) == marker

    assert supervisor.rollback(repo, data_dir, marker, "Dienst startete nicht") is True

    assert sh_git(repo, "rev-parse", "HEAD") == previous
    assert not (repo / "mypkg" / "extra.py").exists()
    assert no_pip and no_pip[0][-1] == str(repo / "requirements.txt")
    assert not (data_dir / supervisor.UPDATE_MARKER).exists()
    notice = json.loads((data_dir / "rollback_notice.json").read_text(encoding="utf-8"))
    assert notice == {"job_id": job_id, "reason": "Dienst startete nicht"}
    db = Database(data_dir / "jarvis.db")
    row = db.query_one("SELECT status, error FROM improvements WHERE id = ?", (job_id,))
    db.close()
    assert row is not None
    assert row["status"] == "rolled_back"
    assert row["error"] == "Automatisch zurückgerollt: Dienst startete nicht"


def test_supervisor_rollback_refuses_without_valid_commit(
    repo: Path, tmp_path: Path, no_pip: list[list[str]]
) -> None:
    head = sh_git(repo, "rev-parse", "HEAD")
    tmp_path.joinpath(supervisor.UPDATE_MARKER).write_text("{}", encoding="utf-8")
    assert supervisor.rollback(repo, tmp_path, {}, "x") is False
    assert supervisor.rollback(repo, tmp_path, {"previous_commit": "deadbeef"}, "x") is False
    assert sh_git(repo, "rev-parse", "HEAD") == head
    assert (tmp_path / supervisor.UPDATE_MARKER).exists()
    assert no_pip == []

    (tmp_path / supervisor.UPDATE_MARKER).write_text("kein json", encoding="utf-8")
    assert supervisor._read_marker(tmp_path) is None
    assert read_update_marker(tmp_path) is None
    assert supervisor.service_command("c.yaml")[-3:] == ["--config", "c.yaml", "service"]
