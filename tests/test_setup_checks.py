"""Einrichtungs-Checkliste (Dashboard/``jarvis setup``) mit echter App und Fake-Anbietern."""

from __future__ import annotations

import builtins
import copy
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from jarvis.core import setup_checks
from jarvis.core.app import JarvisApp
from jarvis.core.config import JarvisConfig
from jarvis.core.hardware import ModelRecommendation
from jarvis.core.setup_checks import CheckItem, missing_ollama_models, run_checks
from jarvis.integrations import claude_code
from jarvis.router.providers.ollama_provider import OllamaProvider
from jarvis.ui import setup_wizard

from .conftest import TEST_ROUTER

OLLAMA_MODELS = [
    {"id": "local-general", "provider": "ollama", "provider_model": "qwen3:8b", "local": True},
    {"id": "gemma3", "provider": "ollama", "local": True},  # ohne Tag → passt zu gemma3:latest
    {"id": "local-big", "provider": "ollama", "provider_model": "llama3:70b", "enabled": False},
]


def _write_config(tmp_path: Path, **overrides: Any) -> Path:
    router = copy.deepcopy(TEST_ROUTER)
    router["providers"].update({"gemini": {}, "ollama": {}, "claude_code": {}})
    router["models"] = [*router["models"], *OLLAMA_MODELS]
    data: dict[str, Any] = {
        "paths": {"data_dir": str(tmp_path / "data")},
        "secrets": {"use_keyring": False},
        "memory": {"embedder": "none"},
        "router": router,
        **overrides,
    }
    config = JarvisConfig.model_validate(data)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _no_real_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Weder ein echter Gemini-Key noch eine echte Claude-Installation fließen ein."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(claude_code, "find_claude_executable", lambda configured=None: None)


@pytest.fixture
async def full_app(tmp_path: Path) -> AsyncIterator[JarvisApp]:
    """App mit Claude Code, Gemini und Ollama (ohne Hardware-Erkennung)."""
    app = JarvisApp.bootstrap(_write_config(tmp_path), role="test", detect=False)
    yield app
    await app.aclose()


@pytest.fixture
async def plain_app(config_file: Path) -> AsyncIterator[JarvisApp]:
    """App nur mit dem Fake-Anbieter aus conftest."""
    app = JarvisApp.bootstrap(config_file, role="test", detect=False)
    yield app
    await app.aclose()


def _by_key(items: list[CheckItem]) -> dict[str, CheckItem]:
    return {item.key: item for item in items}


def _ollama(app: JarvisApp) -> OllamaProvider:
    ollama = app.provider_of_type(OllamaProvider)
    assert ollama is not None
    return ollama


async def _probe_ollama(
    app: JarvisApp, handler: Callable[[httpx.Request], httpx.Response]
) -> OllamaProvider:
    ollama = _ollama(app)
    if ollama._client is not None:  # Client samt Transport neu aufbauen
        await ollama._client.aclose()
        ollama._client = None
    ollama.transport = httpx.MockTransport(handler)
    await ollama.probe()
    return ollama


def _tags(*names: str) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": n} for n in names]})

    return handler


def _offline(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("Verbindung abgelehnt", request=request)


# ---------------------------------------------------------------------- Grundgerüst


async def test_plain_app_reports_only_general_items(plain_app: JarvisApp) -> None:
    items = run_checks(plain_app)
    # Ohne Claude/Gemini/Ollama in der Konfiguration erscheinen deren Punkte nicht.
    assert [i.key for i in items] == ["google", "voice", "git"]
    for item in items:
        assert item.title and item.detail
        assert set(item.to_dict()) == {"key", "title", "ok", "detail", "hint", "optional"}
    assert missing_ollama_models(plain_app) == []


async def test_all_items_have_titles_hints_and_optional_flags(full_app: JarvisApp) -> None:
    items = run_checks(full_app)
    keys = [i.key for i in items]
    assert keys == ["claude_code", "gemini", "ollama", "google", "voice", "git"]
    for item in items:
        assert item.title and item.detail
        if not item.ok:
            assert item.hint, item.key
    optional = {i.key for i in items if i.optional}
    # Pflicht sind nur Claude (Pro-Abo) und Git
    assert optional == {"gemini", "ollama", "google", "voice"}
    json.dumps([i.to_dict() for i in items])  # muss als JSON ans Dashboard gehen


# ---------------------------------------------------------------------- Anbieter


async def test_claude_code_missing_and_ready(
    full_app: JarvisApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = _by_key(run_checks(full_app))["claude_code"]
    assert not claude.ok
    assert "nicht installiert" in claude.detail
    assert "winget" in claude.hint

    monkeypatch.setattr(
        claude_code, "find_claude_executable", lambda configured=None: "/usr/bin/claude"
    )
    claude = _by_key(run_checks(full_app))["claude_code"]
    assert claude.ok and claude.detail == "bereit"


async def test_gemini_key_missing_then_present(
    full_app: JarvisApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    gemini = _by_key(run_checks(full_app))["gemini"]
    assert not gemini.ok and gemini.optional
    assert gemini.detail == "kein Key"
    assert "aistudio.google.com" in gemini.hint

    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")  # Keyring ist im Test aus
    gemini = _by_key(run_checks(full_app))["gemini"]
    assert gemini.ok and gemini.detail == "Key hinterlegt"


async def test_ollama_not_running(full_app: JarvisApp) -> None:
    await _probe_ollama(full_app, _offline)
    ollama = _by_key(run_checks(full_app))["ollama"]
    assert not ollama.ok and ollama.optional
    assert "Ollama läuft nicht" in ollama.detail
    # Ohne laufendes Ollama lässt sich nichts über fehlende Modelle sagen.
    assert missing_ollama_models(full_app) == []


async def test_ollama_missing_models(full_app: JarvisApp) -> None:
    await _probe_ollama(full_app, _tags("gemma3:latest", "nomic-embed-text:latest"))
    # Deaktivierte Modelle (llama3:70b) werden nicht verlangt.
    assert missing_ollama_models(full_app) == ["qwen3:8b"]
    ollama = _by_key(run_checks(full_app))["ollama"]
    assert not ollama.ok
    assert ollama.detail == "Modelle fehlen: qwen3:8b"


async def test_ollama_ready(full_app: JarvisApp) -> None:
    await _probe_ollama(full_app, _tags("qwen3:8b", "gemma3:latest"))
    assert missing_ollama_models(full_app) == []
    ollama = _by_key(run_checks(full_app))["ollama"]
    assert ollama.ok and ollama.detail == "läuft, Modelle bereit"


async def test_missing_models_include_embedding_model(tmp_path: Path) -> None:
    app = JarvisApp.bootstrap(
        _write_config(tmp_path, memory={"embedder": "auto"}), role="test", detect=False
    )
    try:
        app.recommendation = ModelRecommendation(
            tier="test",
            general="qwen3:8b",
            coder="qwen3:8b",
            fast="qwen3:8b",
            embedding="embeddinggemma",
            whisper_model="small",
            whisper_device="cpu",
            whisper_compute_type="int8",
        )
        await _probe_ollama(app, _tags("qwen3:8b", "gemma3:latest"))
        assert missing_ollama_models(app) == ["embeddinggemma"]
        await _probe_ollama(app, _tags("qwen3:8b", "gemma3:latest", "embeddinggemma:latest"))
        assert missing_ollama_models(app) == []
    finally:
        await app.aclose()


# ---------------------------------------------------------------------- Google, Sprache, Git


async def test_google_states(plain_app: JarvisApp) -> None:
    google = _by_key(run_checks(plain_app))["google"]
    assert not google.ok and google.optional
    assert google.detail == "OAuth-Client fehlt"

    plain_app.google.base_dir.mkdir(parents=True, exist_ok=True)
    plain_app.google.client_file.write_text("{}", encoding="utf-8")
    google = _by_key(run_checks(plain_app))["google"]
    assert not google.ok and google.detail == "kein Konto verbunden"

    tokens = plain_app.google.tokens_dir
    tokens.mkdir(parents=True)
    (tokens / "max@gmail.com.json").write_text(
        json.dumps({"account": "max@gmail.com"}), encoding="utf-8"
    )
    google = _by_key(run_checks(plain_app))["google"]
    assert google.ok and google.detail == "max@gmail.com"


@pytest.mark.parametrize(
    ("installed", "enabled", "ok", "detail", "hint"),
    [
        (True, True, True, "Pakete vorhanden", ""),
        (False, True, False, "Sprachpakete fehlen", "Installationsskript"),
        # Nur ausgeschaltet: kein Hinweis auf das Installationsskript
        (True, False, False, "ausgeschaltet", "Einstellungen"),
    ],
    ids=["bereit", "pakete-fehlen", "ausgeschaltet"],
)
async def test_voice_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installed: bool,
    enabled: bool,
    ok: bool,
    detail: str,
    hint: str,
) -> None:
    monkeypatch.setattr(setup_checks, "_module", lambda name: installed)
    app = JarvisApp.bootstrap(
        _write_config(tmp_path, voice={"enabled": enabled}), role="test", detect=False
    )
    try:
        voice = _by_key(run_checks(app))["voice"]
    finally:
        await app.aclose()
    assert voice.ok is ok and voice.optional
    assert detail in voice.detail
    assert hint in voice.hint


async def test_git_missing(plain_app: JarvisApp, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup_checks.shutil, "which", lambda name: None)
    git = _by_key(run_checks(plain_app))["git"]
    assert not git.ok and not git.optional
    assert git.detail == "fehlt"
    assert "Git.Git" in git.hint


async def test_self_improve_item_when_manager_present(plain_app: JarvisApp) -> None:
    class FakeImprovements:
        reason: str | None = "Git fehlt – 'winget install Git.Git'"

        def unavailable_reason(self) -> str | None:
            return self.reason

    fake = FakeImprovements()
    plain_app.improvements = fake  # type: ignore[assignment]
    item = _by_key(run_checks(plain_app))["self_improve"]
    assert not item.ok and item.detail.startswith("Git fehlt")
    fake.reason = None
    item = _by_key(run_checks(plain_app))["self_improve"]
    assert item.ok and item.detail == "bereit"


def test_module_check_handles_odd_names() -> None:
    assert setup_checks._module("json") is True
    assert setup_checks._module("gibt_es_nicht_123") is False
    assert setup_checks._module("gibt_es_nicht_123.unter") is False  # kein ImportError


# ---------------------------------------------------------------------- Assistent: Eingaben


def _answers(monkeypatch: pytest.MonkeyPatch, *answers: str) -> list[str]:
    """Ersetzt input(); liefert die gestellten Fragen zurück."""
    prompts: list[str] = []
    queue = list(answers)

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        return queue.pop(0)

    monkeypatch.setattr(builtins, "input", fake_input)
    return prompts


async def test_ask_uses_default_and_strips(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts = _answers(monkeypatch, "   ", "  Anna  ", "")
    assert await setup_wizard._ask("Name", "Max") == "Max"
    assert await setup_wizard._ask("Name", "Max") == "Anna"
    assert await setup_wizard._ask("Wohnort") == ""
    assert prompts == ["Name [Max]: ", "Name [Max]: ", "Wohnort: "]


@pytest.mark.parametrize(
    ("answer", "default", "expected"),
    [
        ("", True, True),
        ("", False, False),
        ("j", False, True),
        ("JA", False, True),
        ("y", False, True),
        ("Yes", False, True),
        ("n", True, False),
        ("nein", True, False),
        ("vielleicht", True, False),
    ],
)
async def test_yes_answers(
    monkeypatch: pytest.MonkeyPatch, answer: str, default: bool, expected: bool
) -> None:
    prompts = _answers(monkeypatch, answer)
    assert await setup_wizard._yes("Weiter?", default=default) is expected
    assert prompts == [f"Weiter? ({'J/n' if default else 'j/N'}): "]
