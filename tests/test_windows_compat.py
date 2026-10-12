"""Windows-Besonderheiten, soweit sie sich auch unter Linux prüfen lassen.

* ``pythonw.exe`` hat kein ``sys.stdout``/``sys.stderr`` (Verknüpfung, Autostart, Dienst).
* Whisper auf der GPU scheitert unter Windows oft erst beim Erkennen (fehlende CUDA-DLLs).
* Beim Beenden eines Prozesses laufen unter Windows dessen Kindprozesse weiter.
* OneDrive leitet Desktop/Dokumente um – ``~/Documents`` ist dann der falsche Ordner.
* Windows-Pfade werden ohne Rücksicht auf Groß-/Kleinschreibung verglichen.
"""

from __future__ import annotations

import subprocess
import sys
import time
import types
from collections.abc import Iterator
from pathlib import Path, PureWindowsPath
from typing import Any, ClassVar

import numpy as np
import platformdirs
import psutil
import pytest

from jarvis import cli
from jarvis.core.config import SecurityConfig
from jarvis.core.tasks import kill_process_tree
from jarvis.plugins.base import ToolContext
from jarvis.plugins.shell import powershell_run
from jarvis.security import paths
from jarvis.security.paths import PathPolicy, expand_user
from jarvis.security.permissions import Level
from jarvis.voice.engines import SpeechToText

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="nutzt /bin/sh")

# ------------------------------------------------------------------ pythonw.exe


def test_configure_stdio_replaces_missing_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    cli._configure_stdio()
    out, err = sys.stdout, sys.stderr
    try:
        assert out is not None
        assert err is not None
        # Genau daran scheiterte uvicorn.Config(...) unter pythonw.exe (sys.stdout.isatty()).
        from uvicorn.logging import DefaultFormatter

        assert DefaultFormatter(use_colors=None).use_colors is False
        print("landet in os.devnull")
    finally:
        out.close()
        err.close()


def test_configure_stdio_keeps_existing_streams() -> None:
    before = (sys.stdout, sys.stderr)
    cli._configure_stdio()
    assert (sys.stdout, sys.stderr) == before


# ------------------------------------------------------------------ Whisper GPU → CPU


class _FakeWhisperModel:
    created: ClassVar[list[tuple[str, str]]] = []

    def __init__(self, name: str, *, device: str, compute_type: str) -> None:
        self.device = device
        _FakeWhisperModel.created.append((device, compute_type))

    def transcribe(self, audio: Any, **kwargs: Any) -> tuple[Iterator[Any], None]:
        def segments() -> Iterator[Any]:
            # Wie CTranslate2: Laden klappt, der Fehler kommt erst beim Berechnen der Segmente.
            if self.device == "cuda":
                raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
            if self.device == "kaputt":
                raise RuntimeError("echter Fehler")
            yield types.SimpleNamespace(text=" Hallo ")
            yield types.SimpleNamespace(text="Welt ")

        return segments(), None


@pytest.fixture
def fake_whisper(monkeypatch: pytest.MonkeyPatch) -> type[_FakeWhisperModel]:
    module = types.ModuleType("faster_whisper")
    module.WhisperModel = _FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    _FakeWhisperModel.created = []
    return _FakeWhisperModel


def test_whisper_falls_back_to_cpu_when_gpu_fails_while_transcribing(
    fake_whisper: type[_FakeWhisperModel],
) -> None:
    stt = SpeechToText("small", device="cuda", compute_type="float16")
    audio = np.zeros(16_000, dtype=np.float32)
    assert stt.transcribe(audio) == "Hallo Welt"
    assert fake_whisper.created == [("cuda", "float16"), ("cpu", "int8")]
    # Danach bleibt es bei der CPU – kein erneuter GPU-Versuch.
    assert stt.transcribe(audio) == "Hallo Welt"
    assert len(fake_whisper.created) == 2


def test_whisper_errors_on_cpu_are_not_hidden(fake_whisper: type[_FakeWhisperModel]) -> None:
    stt = SpeechToText("small", device="cpu")
    stt._load()
    stt._model.device = "kaputt"
    with pytest.raises(RuntimeError, match="echter Fehler"):
        stt.transcribe(np.zeros(16_000, dtype=np.float32))
    assert fake_whisper.created == [("cpu", "int8")]


# ------------------------------------------------------------------ Prozessbäume


def _gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def _wait_gone(pid: int) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if _gone(pid):
            return True
        time.sleep(0.05)
    return False


@posix_only
def test_kill_process_tree_kills_grandchildren() -> None:
    proc = subprocess.Popen(
        ["sh", "-c", "sleep 60 & echo $!; wait"], stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout is not None
    grandchild = int(proc.stdout.readline())
    try:
        # Fremde Prozesse (hier: das Enkelkind) werden nicht angefasst.
        kill_process_tree(grandchild)
        time.sleep(0.1)
        assert not _gone(grandchild)
        kill_process_tree(proc.pid)
        proc.wait(timeout=5)
        assert _wait_gone(grandchild)
    finally:
        proc.kill()
        proc.wait(timeout=5)
        proc.stdout.close()
        if not _gone(grandchild):
            psutil.Process(grandchild).kill()


@posix_only
async def test_powershell_timeout_kills_process_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "enkel.pid"
    fake = tmp_path / "powershell"
    fake.write_text('#!/bin/sh\nsleep 60 &\necho $! > "$JARVIS_TEST_PIDFILE"\nwait\n')
    fake.chmod(0o755)
    monkeypatch.setenv("JARVIS_TEST_PIDFILE", str(pid_file))
    monkeypatch.setattr("jarvis.plugins.shell._powershell", lambda: str(fake))
    ctx = ToolContext(app=types.SimpleNamespace(workspace=tmp_path))

    result = await powershell_run({"command": "ping -t localhost", "timeout_s": 1}, ctx)

    assert not result.ok
    assert "Abgebrochen nach 1 s" in result.text
    assert _wait_gone(int(pid_file.read_text()))


# ------------------------------------------------------------------ Benutzerordner (OneDrive)


@pytest.fixture
def onedrive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Simuliert Windows 11 mit OneDrive-Sicherung von Desktop und Dokumenten."""
    root = tmp_path / "OneDrive"
    monkeypatch.setattr(platformdirs, "user_documents_dir", lambda: str(root / "Dokumente"))
    monkeypatch.setattr(platformdirs, "user_desktop_dir", lambda: str(root / "Desktop"))
    return root


def test_known_folders_follow_redirection(onedrive: Path) -> None:
    assert expand_user("~/Documents/JARVIS") == onedrive / "Dokumente" / "JARVIS"
    assert expand_user("~\\Dokumente\\a.txt") == onedrive / "Dokumente" / "a.txt"
    assert expand_user("~/desktop") == onedrive / "Desktop"
    # Alles andere wie gehabt.
    assert expand_user("~/Documents-alt/x") == Path.home() / "Documents-alt" / "x"
    assert expand_user("~/.ssh/config") == Path.home() / ".ssh" / "config"
    assert expand_user("Documents/x") == Path("Documents/x")


def test_known_folder_lookup_failure_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> str:
        raise OSError("SHGetKnownFolderPath fehlgeschlagen")

    monkeypatch.setattr(platformdirs, "user_documents_dir", broken)
    assert expand_user("~/Documents/x") == Path.home() / "Documents" / "x"


def test_policy_uses_real_documents_folder(onedrive: Path, tmp_path: Path) -> None:
    data = tmp_path / "data"
    policy = PathPolicy(
        SecurityConfig(write_allowed_dirs=["@workspace", "~/Documents/JARVIS"], protected_dirs=[]),
        workspace=data / "workspace",
        data_dir=data,
        project_root=tmp_path / "projekt",
    )
    target = policy.resolve("~/Dokumente/JARVIS/brief.txt")
    assert target == (onedrive / "Dokumente" / "JARVIS" / "brief.txt").resolve()
    # Der Standardordner aus config.yaml ist der echte (umgeleitete) Dokumente-Ordner.
    assert policy.write_level(target, overwrite=False) is Level.AUTO
    assert policy.write_level(policy.resolve("~/Desktop/x.txt"), overwrite=False) is (Level.CONFIRM)


# ------------------------------------------------------------------ Windows-Pfadvergleiche


def test_windows_path_containment_ignores_case_and_separators() -> None:
    inside = paths._inside
    windows = PureWindowsPath("C:/Windows")
    assert inside(PureWindowsPath(r"c:\windows\System32\drivers\etc\hosts"), windows)
    assert inside(
        PureWindowsPath("C:/PROGRAM FILES/App/x.exe"), PureWindowsPath(r"C:\Program Files")
    )
    assert not inside(PureWindowsPath(r"D:\Windows\x"), windows)
    assert not inside(PureWindowsPath(r"C:\WindowsApps\x"), windows)
