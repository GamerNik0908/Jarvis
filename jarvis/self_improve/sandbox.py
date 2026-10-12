"""Testumgebung für Selbstverbesserungen.

Die Änderungen liegen immer in einer separaten Kopie des Codes (Git-Worktree).
Getestet wird mit ``pytest``:

* Bringt die Änderung keine neuen Pakete mit, laufen die Tests mit der normalen
  Python-Umgebung von JARVIS (schnell).
* Ändert sie ``requirements*.txt``, entsteht eine eigene virtuelle Umgebung
  ("Sandbox"), in der die neuen Pakete installiert und getestet werden. Die
  Hauptumgebung bekommt neue Pakete erst nach deiner Freigabe.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

REQUIREMENT_FILES = ("requirements.txt", "requirements-dev.txt")


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def requirements_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for name in REQUIREMENT_FILES:
        path = root / name
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def base_python() -> str:
    """Python mit Konsole (pythonw.exe kann keine Ausgabe liefern)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.is_file():
            return str(candidate)
    return str(exe)


def test_env(python: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = str(Path(python).parent) + os.pathsep + env.get("PATH", "")
    env["PYTHONUTF8"] = "1"
    # Kein Bytecode-Cache: Nachbesserungen innerhalb derselben Sekunde mit gleicher Dateigröße
    # würden sonst mit veraltetem .pyc getestet.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["JARVIS_DATA_DIR"] = str(Path(tempfile.gettempdir()) / "jarvis-test-data")
    env.pop("JARVIS_CONFIG", None)
    return env


async def _run(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    limit_s: float,
    log: Callable[[str], None] | None,
) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )
    lines: list[str] = []

    async def read() -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            lines.append(line)
            if log is not None:
                log(line)

    try:
        await asyncio.wait_for(asyncio.gather(read(), proc.wait()), limit_s)
    except TimeoutError:
        proc.kill()
        lines.append(f"… abgebrochen nach {limit_s:.0f} s")
        return 124, "\n".join(lines)
    return proc.returncode or 0, "\n".join(lines)


class Sandbox:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.venv = root / "venv"

    @property
    def python(self) -> Path:
        return venv_python(self.venv)

    async def ensure(self, worktree: Path, log: Callable[[str], None]) -> str:
        """Erstellt/aktualisiert die Sandbox-Umgebung passend zu den Requirements."""
        self.root.mkdir(parents=True, exist_ok=True)
        env = test_env(base_python())
        if not self.python.is_file():
            log("Lege Sandbox-Umgebung an (einmalig, dauert einige Minuten) …")
            code, output = await _run(
                [base_python(), "-m", "venv", str(self.venv)],
                cwd=self.root,
                env=env,
                limit_s=600,
                log=None,
            )
            if code != 0:
                raise RuntimeError(f"Sandbox konnte nicht erstellt werden:\n{output[-2000:]}")
        marker = self.root / "requirements.sha256"
        wanted = requirements_hash(worktree)
        if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != wanted:
            req = worktree / "requirements-dev.txt"
            if not req.is_file():
                req = worktree / "requirements.txt"
            log(f"Installiere Pakete in der Sandbox ({req.name}) …")
            code, output = await _run(
                [str(self.python), "-m", "pip", "install", "-q", "-r", str(req)],
                cwd=worktree,
                env=test_env(str(self.python)),
                limit_s=1800,
                log=log,
            )
            if code != 0:
                raise RuntimeError(
                    f"Paketinstallation in der Sandbox fehlgeschlagen:\n{output[-3000:]}"
                )
            marker.write_text(wanted, encoding="utf-8")
        return str(self.python)


async def run_tests(
    worktree: Path,
    python: str,
    *,
    limit_s: float = 900,
    log: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    env = test_env(python)
    # Bei einer editierbaren Installation (pip install -e .) zeigt ``import jarvis`` sonst
    # womöglich auf den Hauptordner – getestet werden soll aber der Code im Worktree.
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(worktree) + (os.pathsep + existing if existing else "")
    code, output = await _run(
        [python, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=worktree,
        env=env,
        limit_s=limit_s,
        log=log,
    )
    return code == 0, output
