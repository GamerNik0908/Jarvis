"""Sicherheitskern: Stufen, Bestätigungen, Not-Aus, Aktionsprotokoll, Schutz- und Pfadregeln."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from jarvis.core.config import SecurityConfig
from jarvis.core.db import Database
from jarvis.core.events import EventBus
from jarvis.security import guard
from jarvis.security.audit import AuditEntry, AuditLog
from jarvis.security.killswitch import KillSwitch, KillSwitchEngaged
from jarvis.security.paths import PathPolicy
from jarvis.security.permissions import (
    ConfirmationBroker,
    Level,
    cancel_quietly,
    describe_arguments,
    stricter,
)
from jarvis.security.redaction import REDACTED

# ------------------------------------------------------------------ Stufen


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (Level.AUTO, Level.AUTO, Level.AUTO),
        (Level.AUTO, Level.CONFIRM, Level.CONFIRM),
        (Level.CONFIRM, Level.AUTO, Level.CONFIRM),
        (Level.CONFIRM, Level.FORBIDDEN, Level.FORBIDDEN),
        (Level.FORBIDDEN, Level.AUTO, Level.FORBIDDEN),
        (Level.FORBIDDEN, Level.FORBIDDEN, Level.FORBIDDEN),
    ],
)
def test_stricter_returns_the_stronger_level(a: Level, b: Level, expected: Level) -> None:
    assert stricter(a, b) is expected


def test_levels_use_the_config_spelling() -> None:
    assert Level("confirm") is Level.CONFIRM
    assert Level.FORBIDDEN == "forbidden"
    assert str(Level.AUTO) == "auto"


def test_describe_arguments_shortens_long_values_and_total() -> None:
    text = describe_arguments({"path": "a.txt", "content": "x" * 500})
    assert text.startswith("path=a.txt, content=")
    assert text.endswith("x" * 117 + "…")
    many = describe_arguments({f"k{i}": "y" * 100 for i in range(10)}, limit=50)
    assert len(many) == 50
    assert many.endswith("…")


async def test_cancel_quietly_swallows_cancellation() -> None:
    task = asyncio.create_task(asyncio.sleep(10))
    await cancel_quietly(task)
    assert task.cancelled()


# ------------------------------------------------------------------ Bestätigungen


async def _wait_for_pending(broker: ConfirmationBroker, count: int = 1) -> list[dict[str, Any]]:
    """Wartet, bis ``count`` Anfragen offen sind."""
    for _ in range(400):
        pending = broker.pending()
        if len(pending) >= count:
            return pending
        await asyncio.sleep(0.005)
    raise AssertionError("Keine offene Bestätigungsanfrage")


async def test_request_and_approve() -> None:
    events = EventBus()
    broker = ConfirmationBroker(events)
    task = asyncio.create_task(
        broker.request(
            tool="files_delete",
            summary="Löschen: a.txt",
            details={"path": "a.txt"},
            session_id="s1",
            source="voice",
        )
    )
    [pending] = await _wait_for_pending(broker)
    assert pending["tool"] == "files_delete"
    assert pending["summary"] == "Löschen: a.txt"
    assert pending["details"] == {"path": "a.txt"}
    assert pending["session_id"] == "s1"
    assert pending["source"] == "voice"
    assert broker.latest() == pending

    assert broker.resolve(pending["id"], True) is True
    assert await task == (True, "approved")
    assert broker.pending() == []
    assert broker.latest() is None

    requested, resolved = events.recent(prefix="confirmation.")
    assert requested.type == "confirmation.requested"
    assert requested.session_id == "s1"
    assert resolved.type == "confirmation.resolved"
    assert resolved.data == {"id": pending["id"], "approved": True, "decision": "approved"}


async def test_request_and_deny() -> None:
    broker = ConfirmationBroker(EventBus())
    task = asyncio.create_task(broker.request(tool="gmail_send", summary="Mail senden"))
    [pending] = await _wait_for_pending(broker)
    broker.resolve(pending["id"], False)
    assert await task == (False, "denied")


def test_resolve_unknown_id_returns_false() -> None:
    assert ConfirmationBroker(EventBus()).resolve("gibt-es-nicht", True) is False


async def test_resolve_from_another_thread() -> None:
    """Tray, Sprache und Web antworten aus fremden Threads."""
    broker = ConfirmationBroker(EventBus())
    task = asyncio.create_task(broker.request(tool="app_close", summary="Schließen"))
    [pending] = await _wait_for_pending(broker)
    assert await asyncio.to_thread(broker.resolve, pending["id"], True) is True
    assert await asyncio.wait_for(task, 2) == (True, "approved")


async def test_no_answer_counts_as_denied() -> None:
    events = EventBus()
    broker = ConfirmationBroker(events, timeout_s=0.05)
    assert await broker.request(tool="x", summary="y") == (False, "timeout")
    assert broker.pending() == []
    assert events.recent(prefix="confirmation.resolved")[-1].data["decision"] == "timeout"


async def test_per_request_timeout_overrides_default() -> None:
    broker = ConfirmationBroker(EventBus(), timeout_s=600)
    approved, decision = await asyncio.wait_for(
        broker.request(tool="x", summary="y", timeout_s=0.05), 2
    )
    assert (approved, decision) == (False, "timeout")


async def test_deny_all_rejects_every_open_request() -> None:
    broker = ConfirmationBroker(EventBus())
    first = asyncio.create_task(broker.request(tool="eins", summary="1"))
    second = asyncio.create_task(broker.request(tool="zwei", summary="2"))
    await _wait_for_pending(broker, 2)
    latest = broker.latest()
    assert latest is not None
    assert latest["tool"] == "zwei"  # die zuletzt gestellte Anfrage

    assert broker.deny_all() == 2
    assert await first == (False, "denied")
    assert await second == (False, "denied")
    assert broker.deny_all() == 0


async def test_cancelled_request_is_reported_as_resolved() -> None:
    """Bricht der Not-Aus die wartende Aufgabe ab, darf kein Dialog offen bleiben."""
    events = EventBus()
    broker = ConfirmationBroker(events)
    task = asyncio.create_task(broker.request(tool="x", summary="y"))
    [pending] = await _wait_for_pending(broker)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert broker.pending() == []
    resolved = events.recent(prefix="confirmation.resolved")
    assert [e.data for e in resolved] == [
        {"id": pending["id"], "approved": False, "decision": "cancelled"}
    ]


@pytest.mark.parametrize(("answer", "decision"), [(True, "approved"), (False, "denied")])
async def test_auto_answer_skips_the_question(answer: bool, decision: str) -> None:
    events = EventBus()
    broker = ConfirmationBroker(events)
    broker.auto_answer = answer
    assert await broker.request(tool="x", summary="y") == (answer, decision)
    assert broker.pending() == []
    assert events.recent() == []


# ------------------------------------------------------------------ Not-Aus


async def test_killswitch_cancels_tracked_tasks_and_runs_callbacks() -> None:
    events = EventBus()
    switch = KillSwitch(events)
    ran: list[str] = []
    async_done = asyncio.Event()

    async def async_cleanup() -> None:
        ran.append("async")
        async_done.set()

    def broken() -> None:
        raise RuntimeError("kaputt")

    switch.on_engage(broken)  # ein fehlerhafter Callback stoppt die anderen nicht
    switch.on_engage(lambda: ran.append("sync"))
    switch.on_engage(async_cleanup)
    task = switch.track(asyncio.create_task(asyncio.sleep(10)))

    switch.engage("Test", by="pytest")
    assert switch.engaged
    assert switch.reason == "Test"
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(async_done.wait(), 1)
    assert ran == ["sync", "async"]
    assert events.recent()[-1].type == "killswitch.engaged"
    assert events.recent()[-1].data == {"reason": "Test", "by": "pytest"}
    with pytest.raises(KillSwitchEngaged, match="Test"):
        switch.check()


async def test_killswitch_forgets_finished_tasks() -> None:
    switch = KillSwitch(EventBus())
    finished = switch.track(asyncio.create_task(asyncio.sleep(0)))
    await finished
    running = switch.track(asyncio.create_task(asyncio.sleep(10)))
    switch.engage()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert not finished.cancelled()


async def test_killswitch_engage_from_other_thread_uses_bound_loop() -> None:
    switch = KillSwitch(EventBus())
    switch.bind_loop(asyncio.get_running_loop())
    task = switch.track(asyncio.create_task(asyncio.sleep(10)))
    await asyncio.to_thread(switch.engage, "Hotkey", by="hotkey")
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert switch.engaged


async def test_killswitch_release() -> None:
    events = EventBus()
    switch = KillSwitch(events)
    switch.check()  # nicht aktiv → keine Ausnahme
    switch.engage("x")
    switch.release(by="web")
    assert not switch.engaged
    assert switch.reason == ""
    switch.check()
    assert events.recent()[-1].type == "killswitch.released"
    assert events.recent()[-1].data == {"by": "web"}


# ------------------------------------------------------------------ Aktionsprotokoll


def test_audit_record_and_recent(db: Database) -> None:
    audit = AuditLog(db)
    first = audit.record(
        AuditEntry(
            source="chat",
            tool="files_read",
            arguments={"path": "a.txt"},
            level="auto",
            decision="auto",
            ok=True,
            result="Inhalt",
            duration_ms=12,
            session_id="s1",
        )
    )
    second = audit.record(
        AuditEntry(source="voice", tool="app_close", arguments={}, level="confirm", decision="x")
    )
    assert second > first

    rows = audit.recent()
    assert [r["id"] for r in rows] == [second, first]  # neueste zuerst
    newest, oldest = rows
    assert newest["ok"] is None  # noch kein Ergebnis
    assert oldest["ok"] == 1
    assert oldest["tool"] == "files_read"
    assert oldest["session_id"] == "s1"
    assert oldest["duration_ms"] == 12
    assert oldest["arguments"] == '{"path": "a.txt"}'
    assert oldest["ts"]
    assert [r["id"] for r in audit.recent(tool="files_read")] == [first]
    assert len(audit.recent(limit=1)) == 1


def test_audit_redacts_secrets_and_limits_size(db: Database) -> None:
    audit = AuditLog(db)
    key = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
    audit.record(
        AuditEntry(
            source="chat",
            tool="web_fetch",
            arguments={"header": f"Bearer {key}", "big": "x" * 5000},
            level="auto",
            decision="auto",
            ok=True,
            result=f"Antwort mit {key} " + "y" * 2000,
        )
    )
    [row] = audit.recent()
    assert key not in row["arguments"]
    assert REDACTED in row["arguments"]
    assert len(row["arguments"]) <= 2000
    assert key not in row["result"]
    assert len(row["result"]) <= 600


def test_audit_failures_since(db: Database) -> None:
    audit = AuditLog(db)
    for ok in (True, False, None, False):
        audit.record(
            AuditEntry(source="chat", tool="t", arguments={}, level="auto", decision="d", ok=ok)
        )
    failures = audit.failures_since("2000-01-01T00:00:00")
    assert len(failures) == 2
    assert all(f["ok"] == 0 for f in failures)
    assert audit.failures_since("2999-01-01T00:00:00") == []
    assert len(audit.failures_since("2000-01-01T00:00:00", limit=1)) == 1


# ------------------------------------------------------------------ Schutzregeln (Selbstverbesserung)


@pytest.mark.parametrize(
    "path",
    [
        "jarvis/security/guard.py",
        "./jarvis/security/paths.py",
        "jarvis\\security\\audit.py",
        "jarvis/security",
        "jarvis/self_improve/manager.py",
        "jarvis/core/supervisor.py",
        ".env",
        ".env.local",
        "config.local.yaml",
        ".git/config",
        ".git",
        ".gitattributes",
    ],
)
def test_protected_paths(path: str) -> None:
    assert guard.is_protected(path)


@pytest.mark.parametrize(
    "path",
    [
        "jarvis/plugins/files.py",
        "jarvis/core/app.py",
        "jarvis/securityx/a.py",
        "config.yaml",
        "tests/test_security.py",
        "docs/.env-anleitung.md",
        "my.env",
    ],
)
def test_unprotected_paths(path: str) -> None:
    assert not guard.is_protected(path)


def test_protected_changes_are_normalized_and_unique() -> None:
    changed = ["./jarvis/security/guard.py", "jarvis/security/guard.py", "README.md", ".env"]
    assert guard.protected_changes(changed) == [".env", "jarvis/security/guard.py"]


def test_violations_for_protected_files() -> None:
    assert guard.violations(["jarvis/plugins/neu.py", "tests/test_neu.py"]) == []
    assert guard.violations(["jarvis/plugins/neu.py", "jarvis/security/paths.py"]) == [
        "geschützte Datei geändert: jarvis/security/paths.py"
    ]


_CONFIG_BEFORE = """
assistant: {name: JARVIS}
security: {confirm_timeout_s: 600}
budget: {daily_limit_usd: 1.0}
self_improve: {auto_merge: never}
secrets: {use_keyring: true}
"""


@pytest.mark.parametrize("section", ["security", "budget", "self_improve", "secrets"])
def test_violations_for_protected_config_sections(section: str) -> None:
    after = _CONFIG_BEFORE.replace(f"{section}: {{", f"{section}: {{neu: 1, ")
    problems = guard.violations(["config.yaml"], config_before=_CONFIG_BEFORE, config_after=after)
    assert problems == [f"geschützter Konfigurationsabschnitt geändert: {section}"]


def test_unprotected_config_section_may_change() -> None:
    after = _CONFIG_BEFORE.replace("name: JARVIS", "name: FRIDAY")
    assert guard.violations(["config.yaml"], config_before=_CONFIG_BEFORE, config_after=after) == []


def test_config_sections_are_only_checked_when_config_changed() -> None:
    after = _CONFIG_BEFORE.replace("600", "5")
    assert guard.violations(["README.md"], config_before=_CONFIG_BEFORE, config_after=after) == []


def test_deleted_or_broken_config_counts_as_change() -> None:
    assert guard.config_section_changes(_CONFIG_BEFORE, None) == [
        "security",
        "budget",
        "self_improve",
        "secrets",
    ]
    assert guard.config_section_changes(_CONFIG_BEFORE, "security: [unclosed") == [
        "security",
        "budget",
        "self_improve",
        "secrets",
    ]
    assert guard.config_section_changes(None, None) == []


def test_violations_accept_a_generator() -> None:
    """Die Dateiliste wird zweimal durchlaufen – auch ein Generator muss reichen."""
    after = _CONFIG_BEFORE.replace("600", "5")
    changed = (name for name in ["config.yaml"])
    assert guard.violations(changed, config_before=_CONFIG_BEFORE, config_after=after) == [
        "geschützter Konfigurationsabschnitt geändert: security"
    ]


# ------------------------------------------------------------------ Pfadregeln für Werkzeuge


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    dirs = {
        "data": tmp_path / "data",
        "workspace": tmp_path / "data" / "workspace",
        "allowed": tmp_path / "erlaubt",
        "system": tmp_path / "system",
        "project": tmp_path / "projekt",
        "elsewhere": tmp_path / "anderswo",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


@pytest.fixture
def policy(layout: dict[str, Path]) -> PathPolicy:
    config = SecurityConfig(
        write_allowed_dirs=["@workspace", str(layout["allowed"])],
        protected_dirs=[str(layout["system"])],
    )
    return PathPolicy(
        config,
        workspace=layout["workspace"],
        data_dir=layout["data"],
        project_root=layout["project"],
    )


def test_resolve_relative_paths_against_workspace(
    policy: PathPolicy, layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = layout["workspace"].resolve()
    assert policy.resolve("notiz.txt") == workspace / "notiz.txt"
    assert policy.resolve('  "unter/ordner/a.md"  ') == workspace / "unter" / "ordner" / "a.md"
    assert policy.resolve("unter/../b.txt") == workspace / "b.txt"
    assert policy.resolve(str(layout["allowed"] / "c.txt")) == layout["allowed"].resolve() / "c.txt"
    assert policy.resolve("~/x.txt") == (Path.home() / "x.txt").resolve()
    monkeypatch.setenv("JARVIS_TEST_ORDNER", str(layout["elsewhere"]))
    assert policy.resolve("$JARVIS_TEST_ORDNER/d.txt") == layout["elsewhere"].resolve() / "d.txt"


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        "server.KEY",
        "cert.pem",
        "id_rsa",
        "id_rsa.pub",
        "google_credentials.json",
        "my_token_cache.json",
    ],
)
def test_secret_file_names(policy: PathPolicy, layout: dict[str, Path], name: str) -> None:
    path = layout["elsewhere"] / name
    assert policy.is_secret(path)
    assert policy.read_level(path) is Level.FORBIDDEN
    assert policy.write_level(path, overwrite=False) is Level.FORBIDDEN
    assert policy.delete_level(path) is Level.FORBIDDEN


def test_secret_directories(policy: PathPolicy, layout: dict[str, Path]) -> None:
    assert policy.is_secret(layout["data"].resolve() / "google" / "tokens" / "konto.json")
    assert policy.is_secret(layout["data"].resolve() / "api_token")
    assert policy.is_secret(Path.home() / ".ssh" / "config")
    assert policy.is_secret(Path.home() / ".claude")


@pytest.mark.parametrize("name", ["notizen.txt", "token.txt", "bericht.json", "env.md"])
def test_normal_files_are_readable(policy: PathPolicy, layout: dict[str, Path], name: str) -> None:
    path = layout["elsewhere"] / name
    assert not policy.is_secret(path)
    assert policy.read_level(path) is Level.AUTO


def test_protected_locations(policy: PathPolicy, layout: dict[str, Path]) -> None:
    data = layout["data"].resolve()
    assert policy.is_protected(layout["system"].resolve() / "treiber.sys")
    assert policy.is_protected(layout["project"].resolve() / "jarvis" / "core" / "app.py")
    assert policy.is_protected(data / "jarvis.db")
    assert policy.is_protected(data / "logs" / "jarvis-chat.log")
    # Nur der Arbeitsordner im Datenordner ist frei.
    assert not policy.is_protected(data / "workspace" / "notiz.txt")
    assert not policy.is_protected(layout["elsewhere"].resolve() / "a.txt")


def test_write_levels(policy: PathPolicy, layout: dict[str, Path]) -> None:
    workspace = layout["workspace"].resolve()
    allowed = layout["allowed"].resolve()
    # Neue Dateien in freigegebenen Ordnern: ohne Rückfrage.
    assert policy.write_level(workspace / "neu.txt", overwrite=False) is Level.AUTO
    assert policy.write_level(allowed / "sub" / "neu.txt", overwrite=False) is Level.AUTO
    # Überschreiben oder anderswo schreiben: mit Bestätigung.
    assert policy.write_level(workspace / "alt.txt", overwrite=True) is Level.CONFIRM
    assert policy.write_level(layout["elsewhere"].resolve() / "x.txt", overwrite=False) is (
        Level.CONFIRM
    )
    # Geschützte Orte und Secrets: nie.
    assert policy.write_level(layout["data"].resolve() / "jarvis.db", overwrite=False) is (
        Level.FORBIDDEN
    )
    assert policy.write_level(layout["system"].resolve() / "x.dll", overwrite=False) is (
        Level.FORBIDDEN
    )
    assert policy.write_level(workspace / ".env", overwrite=False) is Level.FORBIDDEN


def test_delete_levels(policy: PathPolicy, layout: dict[str, Path]) -> None:
    assert policy.delete_level(layout["workspace"].resolve() / "a.txt") is Level.CONFIRM
    assert policy.delete_level(layout["elsewhere"].resolve() / "a.txt") is Level.CONFIRM
    assert policy.delete_level(layout["project"].resolve() / "README.md") is Level.FORBIDDEN
    assert policy.delete_level(layout["data"].resolve() / "jarvis.db") is Level.FORBIDDEN


def test_symlink_to_secret_is_resolved(policy: PathPolicy, layout: dict[str, Path]) -> None:
    secret = layout["elsewhere"] / ".env"
    secret.write_text("KEY=1", encoding="utf-8")
    link = layout["workspace"] / "harmlos.txt"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("Symlinks werden hier nicht unterstützt")
    assert policy.read_level(policy.resolve("harmlos.txt")) is Level.FORBIDDEN
