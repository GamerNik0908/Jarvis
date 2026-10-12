"""End-to-End über die Kommandozeile – mit dem Fake-Anbieter statt echter API."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis.cli import EXIT_CONFIG, EXIT_ERROR, EXIT_OK, main
from jarvis.core.app import JarvisApp


def test_ask_streams_answer_and_records_cost(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--config", str(config_file), "ask", "Wie", "geht's?"]) == EXIT_OK
    captured = capsys.readouterr()
    assert "Hallo Welt" in captured.out
    assert "fake-big" in captured.err  # Nutzungszeile auf stderr

    app = JarvisApp.bootstrap(config_file, role="test")
    try:
        assert app.costs.spent_today() > 0
    finally:
        app.db.close()


def test_ask_with_unknown_model_fails_cleanly(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--config", str(config_file), "ask", "x", "--model", "nope"])
    assert code == EXIT_ERROR
    assert "unbekanntes Modell" in capsys.readouterr().err


def test_hw_json(config_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config", str(config_file), "hw", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["hardware"]["ram_total_gb"] > 0
    assert payload["recommendation"]["tier"]


def test_check_models_and_cost(config_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config", str(config_file), "check"]) == EXIT_OK
    assert "Alles bereit" in capsys.readouterr().out
    assert main(["--config", str(config_file), "models"]) == EXIT_OK
    assert "fake-small" in capsys.readouterr().out
    assert main(["--config", str(config_file), "cost"]) == EXIT_OK


def test_invalid_config_returns_config_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "config.yaml"
    bad.write_text("router:\n  unbekannt: 1\n", encoding="utf-8")
    assert main(["--config", str(bad), "check"]) == EXIT_CONFIG
    assert "router.unbekannt" in capsys.readouterr().err


def test_chat_non_interactive(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    inputs = iter(["/hilfe", "/aufgabe simple", "Hallo", "/kosten", "/nutze fake-big", "/ende"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    assert main(["--config", str(config_file), "chat"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Aufgabenart: simple" in out
    assert "Hallo Welt" in out
    assert "fake-small" in out  # Route "simple" beginnt mit fake-small
    assert "Feste Modellwahl" in out
