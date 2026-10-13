"""Supervisor: hält den JARVIS-Dienst am Laufen und rollt fehlerhafte Updates zurück.

``jarvis run`` startet diesen Supervisor. Er startet den eigentlichen Dienst
(``jarvis service``) als Kindprozess und reagiert auf dessen Ende:

* Exit-Code 0 → normal beendet, Supervisor endet auch.
* Exit-Code 75 → Neustart gewünscht (z. B. nach einer übernommenen Verbesserung).
* Absturz kurz nach einem Update, bevor der Dienst "gesund" gemeldet hat →
  automatischer Rollback auf den vorherigen Git-Stand und Neustart.
* Sonstiger Absturz → Neustart mit Wartezeit (max. 5 Versuche in 10 Minuten).

Diese Datei ist geschützt und wird von der Selbstverbesserung nie verändert.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)

RESTART_EXIT_CODE = 75
UPDATE_MARKER = "update_pending.json"
HEALTHY_MARKER = "service_healthy"
_CRASH_WINDOW_S = 600
_MAX_CRASHES = 5


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )


def rollback(repo: Path, data_dir: Path, marker: dict[str, object], reason: str) -> bool:
    """Setzt den Code auf den Stand vor dem Update zurück und markiert die Verbesserung."""
    previous = str(marker.get("previous_commit") or "")
    if not previous:
        return False
    result = _git(repo, "reset", "--hard", previous)
    if result.returncode != 0:
        log.error("Rollback fehlgeschlagen: %s", result.stderr.strip())
        return False
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "-r", str(repo / "requirements.txt")],
        cwd=str(repo),
        check=False,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )
    db_path = Path(str(marker.get("db") or data_dir / "jarvis.db"))
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE improvements SET status = 'rolled_back', error = ? WHERE id = ?",
                (f"Automatisch zurückgerollt: {reason}", marker.get("job_id")),
            )
    except sqlite3.Error:
        log.exception("Status der Verbesserung konnte nicht aktualisiert werden")
    (data_dir / UPDATE_MARKER).unlink(missing_ok=True)
    (data_dir / "rollback_notice.json").write_text(
        json.dumps({"job_id": marker.get("job_id"), "reason": reason}), encoding="utf-8"
    )
    log.warning("Update #%s zurückgerollt (%s)", marker.get("job_id"), reason)
    return True


def _read_marker(data_dir: Path) -> dict[str, object] | None:
    path = data_dir / UPDATE_MARKER
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def service_command(config_path: str | None) -> list[str]:
    args = [sys.executable, "-m", "jarvis"]
    if config_path:
        args += ["--config", config_path]
    args.append("service")
    return args


def _wait_interruptible(proc: subprocess.Popen[bytes]) -> int:
    """Wartet auf das Prozessende, bleibt dabei aber für Strg+C erreichbar.

    Unter Windows blockiert ``proc.wait()`` ohne Timeout in ``WaitForSingleObject(INFINITE)``
    – ein KeyboardInterrupt käme erst an, wenn der Dienst von selbst endet.
    """
    while True:
        try:
            return proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            continue


def supervise(repo: Path, data_dir: Path, *, config_path: str | None = None) -> int:
    """Endlosschleife des Supervisors. Liefert den Exit-Code für den Prozess."""
    data_dir.mkdir(parents=True, exist_ok=True)
    crashes: list[float] = []
    while True:
        (data_dir / HEALTHY_MARKER).unlink(missing_ok=True)
        started = time.monotonic()
        env = dict(os.environ)
        env["JARVIS_SUPERVISED"] = "1"
        proc = subprocess.Popen(
            service_command(config_path),
            cwd=str(repo),
            env=env,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
        try:
            code = _wait_interruptible(proc)
        except KeyboardInterrupt:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
            return 0
        healthy = (data_dir / HEALTHY_MARKER).is_file()
        if code == 0:
            return 0
        if code == RESTART_EXIT_CODE:
            log.info("Neustart angefordert")
            continue
        marker = _read_marker(data_dir)
        if (
            marker is not None
            and not healthy
            and rollback(repo, data_dir, marker, f"Dienst startete nicht (Exit-Code {code})")
        ):
            continue
        now = time.monotonic()
        crashes = [t for t in crashes if now - t < _CRASH_WINDOW_S] + [now]
        if len(crashes) >= _MAX_CRASHES:
            log.error("JARVIS stürzt wiederholt ab – Supervisor gibt auf. Siehe Logdateien.")
            return code or 1
        wait = min(5 * len(crashes), 60)
        log.warning(
            "Dienst beendet (Exit-Code %s, lief %.0f s) – Neustart in %s s",
            code,
            now - started,
            wait,
        )
        time.sleep(wait)
