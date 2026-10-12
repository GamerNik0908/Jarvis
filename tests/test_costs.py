from __future__ import annotations

from datetime import datetime

import pytest

from jarvis.core.config import BudgetConfig, ModelConfig
from jarvis.core.db import MIGRATIONS, OPTIONAL_MIGRATIONS, Database
from jarvis.router.costs import CostTracker, UsageRecord, calculate_cost
from jarvis.router.errors import BudgetExceededError

from .conftest import FrozenClock


def _model(**overrides: object) -> ModelConfig:
    data: dict[str, object] = {
        "id": "m",
        "provider": "p",
        "input_cost_per_mtok": 4.0,
        "output_cost_per_mtok": 20.0,
    }
    data.update(overrides)
    return ModelConfig.model_validate(data)


def test_basic_cost() -> None:
    cost = calculate_cost(_model(), input_tokens=1_000_000, output_tokens=500_000)
    assert cost == pytest.approx(4.0 + 10.0)


def test_cache_prices_use_explicit_or_default_factors() -> None:
    explicit = _model(cache_read_cost_per_mtok=0.2)
    assert calculate_cost(
        explicit, input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000
    ) == pytest.approx(0.2)
    default = _model()
    assert calculate_cost(
        default, input_tokens=0, output_tokens=0, cache_write_tokens=1_000_000
    ) == pytest.approx(5.0)  # 1.25 × Eingabepreis


def test_long_context_pricing() -> None:
    model = _model(
        input_cost_per_mtok=0.1,
        output_cost_per_mtok=0.5,
        long_context_threshold_tokens=100_000,
        long_input_cost_per_mtok=0.5,
        long_output_cost_per_mtok=2.5,
    )
    short = calculate_cost(model, input_tokens=100_000, output_tokens=1_000_000)
    long = calculate_cost(model, input_tokens=100_001, output_tokens=1_000_000)
    assert short == pytest.approx(0.01 + 0.5)
    assert long == pytest.approx(100_001 * 0.5 / 1e6 + 2.5)


def test_spending_is_grouped_by_local_day_and_month(costs: CostTracker, clock: FrozenClock) -> None:
    costs.record(UsageRecord(provider="p", model="a", task="chat", cost_usd=0.25))
    costs.record(UsageRecord(provider="p", model="b", task="chat", cost_usd=0.5))
    assert costs.spent_today() == pytest.approx(0.75)

    clock.now = datetime(2026, 10, 13, 9, 0).astimezone()
    costs.record(UsageRecord(provider="p", model="a", task="chat", cost_usd=0.1))
    assert costs.spent_today() == pytest.approx(0.1)
    assert costs.spent_this_month() == pytest.approx(0.85)

    breakdown = {row.model: row for row in costs.breakdown("month")}
    assert breakdown["a"].calls == 2
    assert breakdown["a"].cost_usd == pytest.approx(0.35)


def test_budget_status_warning_and_limit(costs: CostTracker) -> None:
    costs.record(UsageRecord(provider="p", model="a", task="chat", cost_usd=0.85))
    status = costs.ensure_within_budget()
    assert status.warning and not status.exceeded

    costs.record(UsageRecord(provider="p", model="a", task="chat", cost_usd=0.2))
    with pytest.raises(BudgetExceededError, match="Tagesbudget erreicht") as info:
        costs.ensure_within_budget()
    assert info.value.status.daily_exceeded


def test_zero_budget_blocks_immediately(db: Database, clock: FrozenClock) -> None:
    tracker = CostTracker(db, BudgetConfig(daily_limit_usd=0, monthly_limit_usd=0), clock=clock)
    assert tracker.status().exceeded


def test_project_spending(costs: CostTracker) -> None:
    costs.record(UsageRecord(provider="p", model="a", task="code", cost_usd=0.3, project_id="x"))
    costs.record(UsageRecord(provider="p", model="a", task="code", cost_usd=0.2))
    assert costs.spent_for_project("x") == pytest.approx(0.3)


def test_migrations_are_idempotent(db: Database) -> None:
    steps = [*MIGRATIONS, *OPTIONAL_MIGRATIONS]
    latest = max(version for version, _ in steps)
    assert db.migrate() == latest
    assert db.migrate() == latest
    row = db.query_one("SELECT COUNT(*) FROM schema_version")
    assert row is not None and row[0] == len(steps)
    assert db.has_table("memories_fts")
