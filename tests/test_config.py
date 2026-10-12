from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.core.config import (
    PROJECT_ROOT,
    ConfigError,
    PathsConfig,
    RouterConfig,
    deep_merge,
    load_config,
)
from jarvis.core.types import TaskKind


def test_shipped_config_is_valid() -> None:
    loaded = load_config(PROJECT_ROOT / "config.yaml")
    config = loaded.config
    assert config.assistant.name == "JARVIS"
    assert TaskKind.CHAT in config.router.routes
    # Jede Aufgabenart hat eine eigene Route.
    assert set(config.router.routes) == set(TaskKind)
    for targets in config.router.routes.values():
        for target in targets:
            model = config.router.model(target.model)
            assert model is not None and model.enabled


def test_local_override_is_deep_merged(tmp_path: Path) -> None:
    base = tmp_path / "config.yaml"
    base.write_text(
        "assistant:\n  name: JARVIS\n  user_name: ''\nbudget:\n  daily_limit_usd: 2\n",
        encoding="utf-8",
    )
    (tmp_path / "config.local.yaml").write_text(
        "assistant:\n  user_name: Nik\nbudget:\n  monthly_limit_usd: 99\n", encoding="utf-8"
    )
    loaded = load_config(base)
    assert loaded.local_path == tmp_path / "config.local.yaml"
    assert loaded.config.assistant.name == "JARVIS"
    assert loaded.config.assistant.user_name == "Nik"
    assert loaded.config.budget.daily_limit_usd == 2
    assert loaded.config.budget.monthly_limit_usd == 99


def test_deep_merge_replaces_lists() -> None:
    merged = deep_merge({"a": {"b": 1, "c": [1, 2]}}, {"a": {"c": [3]}, "d": 4})
    assert merged == {"a": {"b": 1, "c": [3]}, "d": 4}


def test_unknown_key_is_reported_with_location(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("budget:\n  daily_limt_usd: 5\n", encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_config(path)
    assert "budget.daily_limt_usd" in str(info.value)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="nicht gefunden"):
        load_config(tmp_path / "fehlt.yaml")


def test_invalid_yaml_raises(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("budget: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML"):
        load_config(path)


def test_route_to_unknown_model_is_rejected() -> None:
    with pytest.raises(ValueError, match="unbekanntes Modell"):
        RouterConfig.model_validate(
            {
                "providers": {"fake": {}},
                "models": [{"id": "a", "provider": "fake"}],
                "routes": {"chat": ["b"]},
            }
        )


def test_model_with_unknown_provider_is_rejected() -> None:
    with pytest.raises(ValueError, match="unbekannten Anbieter"):
        RouterConfig.model_validate(
            {"models": [{"id": "a", "provider": "nope"}], "routes": {"chat": ["a"]}}
        )


def test_plain_string_route_targets_are_accepted(router_config: RouterConfig) -> None:
    targets = router_config.routes[TaskKind.CHAT]
    assert [t.model for t in targets] == ["fake-big", "fake-small"]
    assert targets[0].effort == "low"
    assert targets[1].effort is None


def test_data_dir_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("JARVIS_DATA_DIR", raising=False)
    expected = (tmp_path / "daten").resolve()
    assert PathsConfig(data_dir="daten").resolve_data_dir(tmp_path) == expected
    auto = PathsConfig(data_dir="auto").resolve_data_dir(tmp_path)
    assert auto.name == "Jarvis"
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "override"))
    assert (
        PathsConfig(data_dir="auto").resolve_data_dir(tmp_path) == (tmp_path / "override").resolve()
    )
