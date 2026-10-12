"""Weitere Router-Tests: Werkzeuge, private Daten, Pausen, Abo-Kosten, Budget 0, Katalogpflege."""

from __future__ import annotations

from typing import Any

import pytest

from jarvis.core.config import BudgetConfig, RouterConfig
from jarvis.core.db import Database
from jarvis.core.types import TaskKind
from jarvis.router import router as router_module
from jarvis.router.base import ChatMessage, ChatResponse, ToolCall, ToolSpec, UsageSegment
from jarvis.router.costs import CostTracker
from jarvis.router.errors import (
    BudgetExceededError,
    NoModelAvailableError,
    ProviderAuthError,
    ProviderUnavailableError,
)
from jarvis.router.router import ModelRouter

from .conftest import TEST_ROUTER, FakeProvider, FrozenClock

HELLO = [ChatMessage(role="user", content="Hallo")]
TOOL = ToolSpec(name="uhrzeit", description="Aktuelle Uhrzeit", parameters={"type": "object"})


class FakeTime:
    """Ersetzt das ``time``-Modul im Router, damit Pausen ohne Warten ablaufen."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def fake_time(monkeypatch: pytest.MonkeyPatch) -> FakeTime:
    clock = FakeTime()
    monkeypatch.setattr(router_module, "time", clock)
    return clock


def _rows(costs: CostTracker) -> list[dict[str, Any]]:
    return [dict(r) for r in costs._db.query("SELECT * FROM model_usage ORDER BY id")]


def _config(**changes: Any) -> RouterConfig:
    data: dict[str, Any] = {**TEST_ROUTER, **changes}
    return RouterConfig.model_validate(data)


def _response(model: str, **overrides: Any) -> ChatResponse:
    data: dict[str, Any] = {
        "text": "fertig",
        "provider": "fake",
        "model": model,
        "requested_model": model,
        "stop_reason": "end_turn",
        "usage": [UsageSegment(model=model, input_tokens=1000, output_tokens=500)],
    }
    data.update(overrides)
    return ChatResponse(**data)


# ---------------------------------------------------------------- Werkzeuge


def test_plan_with_tools_skips_models_without_tool_support(router: ModelRouter) -> None:
    usable, skipped = router.plan(TaskKind.SIMPLE, needs_tools=True)
    assert [t.model for t in usable] == ["fake-small"]
    assert skipped == ["fake-free: keine Werkzeug-Unterstützung"]
    usable, skipped = router.plan(TaskKind.SIMPLE)
    assert [t.model for t in usable] == ["fake-small", "fake-free"]
    assert skipped == []


async def test_tools_are_passed_to_capable_model(
    router: ModelRouter, fake_provider: FakeProvider
) -> None:
    response = await router.complete(
        HELLO, task=TaskKind.SIMPLE, tools=[TOOL], session_id="sitzung-1"
    )
    assert response.model == "fake-small"
    [request] = fake_provider.requests
    assert list(request.tools) == [TOOL]
    assert request.session_id == "sitzung-1"


async def test_tool_request_does_not_fall_back_to_model_without_tools(
    router: ModelRouter, fake_provider: FakeProvider
) -> None:
    fake_provider.behaviours["fake-small"] = ProviderUnavailableError("down", provider="fake")
    with pytest.raises(NoModelAvailableError) as info:
        await router.complete(HELLO, task=TaskKind.SIMPLE, tools=[TOOL])
    assert [r.model for r in fake_provider.requests] == ["fake-small"]  # fake-free nie versucht
    assert "fake-free: keine Werkzeug-Unterstützung" in info.value.reasons
    assert any(r.startswith("fake-small: down") for r in info.value.reasons)


async def test_pinned_model_without_tool_support(
    router: ModelRouter, fake_provider: FakeProvider
) -> None:
    with pytest.raises(NoModelAvailableError, match="keine Werkzeug-Unterstützung"):
        await router.complete(HELLO, pinned_model="fake-free", tools=[TOOL])
    assert fake_provider.requests == []
    # Ohne Werkzeuge ist dasselbe Modell nutzbar.
    assert (await router.complete(HELLO, pinned_model="fake-free")).model == "fake-free"


async def test_scripted_tool_calls_reach_caller(
    router: ModelRouter, fake_provider: FakeProvider
) -> None:
    call = ToolCall(id="c1", name="uhrzeit", arguments={"zone": "Berlin"})
    fake_provider.scripted["fake-big"] = [
        _response("fake-big", text="", tool_calls=[call], stop_reason="tool_use")
    ]
    chunks = [c async for c in router.stream(HELLO, tools=[TOOL])]
    assert len(chunks) == 1  # kein Text, nur die fertige Antwort
    response = chunks[0].response
    assert response is not None
    assert response.tool_calls == [call]
    assert response.stop_reason == "tool_use"
    # Danach antwortet das Modell wieder normal.
    assert (await router.complete(HELLO, tools=[TOOL])).text == "Hallo Welt"


# ---------------------------------------------------------------- Private Daten


async def test_sensitive_requests_skip_untrusted_models(
    router: ModelRouter, fake_provider: FakeProvider
) -> None:
    with pytest.raises(NoModelAvailableError) as info:
        await router.complete(HELLO, sensitive=True)
    assert fake_provider.requests == []
    assert info.value.reasons == [
        "fake-big: nicht für private Daten freigegeben",
        "fake-small: nicht für private Daten freigegeben",
    ]


async def test_sensitive_requests_use_trusted_model(
    router: ModelRouter, fake_provider: FakeProvider
) -> None:
    response = await router.complete(HELLO, task=TaskKind.SIMPLE, sensitive=True)
    assert response.model == "fake-free"
    assert [r.model for r in fake_provider.requests] == ["fake-free"]


async def test_sensitive_pinned_untrusted_model_is_refused(router: ModelRouter) -> None:
    with pytest.raises(NoModelAvailableError, match="private Daten"):
        await router.complete(HELLO, pinned_model="fake-big", sensitive=True)


async def test_sensitive_with_tools_needs_trusted_tool_model(
    router: ModelRouter, fake_provider: FakeProvider
) -> None:
    with pytest.raises(NoModelAvailableError) as info:
        await router.complete(HELLO, task=TaskKind.SIMPLE, sensitive=True, tools=[TOOL])
    assert info.value.reasons == [
        "fake-small: nicht für private Daten freigegeben",
        "fake-free: keine Werkzeug-Unterstützung",
    ]
    model = router.model("fake-free")
    assert model is not None
    router.replace_model(model.model_copy(update={"supports_tools": True}))
    response = await router.complete(HELLO, task=TaskKind.SIMPLE, sensitive=True, tools=[TOOL])
    assert response.model == "fake-free"


# ---------------------------------------------------------------- Pausen (Cooldown)


async def test_rate_limit_pauses_model(
    router: ModelRouter, fake_provider: FakeProvider, fake_time: FakeTime
) -> None:
    fake_provider.behaviours["fake-big"] = ProviderUnavailableError(
        "Kontingent erschöpft", provider="fake", retry_after_s=120
    )
    response = await router.complete(HELLO)
    assert response.model == "fake-small"
    assert response.router_fallbacks == ["fake-big"]
    assert router.cooldown_remaining("fake-big") == pytest.approx(120)
    assert router.cooldown_remaining("fake-small") == 0.0
    model = router.model("fake-big")
    assert model is not None
    assert "pausiert bis" in (router.model_unavailable_reason(model) or "")
    usable, skipped = router.plan(TaskKind.CHAT)
    assert [t.model for t in usable] == ["fake-small"]
    assert skipped[0].startswith("fake-big: Kontingent erschöpft – pausiert bis")

    # Während der Pause wird fake-big gar nicht erst angefragt.
    fake_time.now += 60
    assert router.cooldown_remaining("fake-big") == pytest.approx(60)
    assert (await router.complete(HELLO)).model == "fake-small"
    assert [r.model for r in fake_provider.requests] == ["fake-big", "fake-small", "fake-small"]

    # Nach Ablauf ist das Modell wieder dran.
    fake_time.now += 61
    assert router.cooldown_remaining("fake-big") == 0.0
    assert router.model_unavailable_reason(model) is None
    del fake_provider.behaviours["fake-big"]
    assert (await router.complete(HELLO)).model == "fake-big"


async def test_unavailable_without_retry_after_does_not_pause(
    router: ModelRouter, fake_provider: FakeProvider, fake_time: FakeTime
) -> None:
    fake_provider.behaviours["fake-big"] = ProviderUnavailableError("Timeout", provider="fake")
    assert (await router.complete(HELLO)).model == "fake-small"
    assert router.cooldown_remaining("fake-big") == 0.0
    await router.complete(HELLO)
    assert [r.model for r in fake_provider.requests] == [
        "fake-big",
        "fake-small",
        "fake-big",
        "fake-small",
    ]


async def test_pinned_model_rate_limit_also_pauses(
    router: ModelRouter, fake_provider: FakeProvider, fake_time: FakeTime
) -> None:
    fake_provider.behaviours["fake-small"] = ProviderUnavailableError(
        "Limit", provider="fake", retry_after_s=30
    )
    with pytest.raises(ProviderUnavailableError):
        await router.complete(HELLO, pinned_model="fake-small")
    with pytest.raises(NoModelAvailableError, match="Kontingent erschöpft"):
        await router.complete(HELLO, pinned_model="fake-small")
    assert len(fake_provider.requests) == 1


async def test_rate_limit_after_partial_output_still_pauses(
    router: ModelRouter, fake_provider: FakeProvider, fake_time: FakeTime
) -> None:
    fake_provider.behaviours["fake-big"] = ["Teil"]
    fake_provider.fail_after["fake-big"] = ProviderUnavailableError(
        "Limit", provider="fake", retry_after_s=300
    )
    with pytest.raises(ProviderUnavailableError):
        await router.complete(HELLO)
    assert router.cooldown_remaining("fake-big") == pytest.approx(300)


# ---------------------------------------------------------------- Abo-Kosten


async def test_subscription_response_costs_nothing(
    router: ModelRouter, fake_provider: FakeProvider, costs: CostTracker
) -> None:
    fake_provider.scripted["fake-big"] = [
        _response(
            "fake-big",
            subscription=True,
            usage=[
                UsageSegment(
                    model="fake-big",
                    input_tokens=1_000_000,
                    output_tokens=1_000_000,
                    cache_read_tokens=500,
                    cache_write_tokens=100,
                )
            ],
        )
    ]
    response = await router.complete(HELLO)
    assert response.model == "fake-big"
    assert response.cost_usd == 0.0
    [row] = _rows(costs)
    assert row["cost_usd"] == 0.0
    assert (row["input_tokens"], row["output_tokens"]) == (1_000_000, 1_000_000)
    assert (row["cache_read_tokens"], row["cache_write_tokens"]) == (500, 100)
    assert costs.spent_today() == 0.0


async def test_subscription_model_in_catalog_costs_nothing(
    fake_provider: FakeProvider, costs: CostTracker
) -> None:
    models = [dict(m) for m in TEST_ROUTER["models"]]  # type: ignore[union-attr]
    models[0]["subscription"] = True  # fake-big über ein Abo
    router = ModelRouter(_config(models=models), {"fake": fake_provider}, costs)
    response = await router.complete(HELLO)
    assert response.model == "fake-big"
    assert response.cost_usd == 0.0
    assert costs.spent_today() == 0.0


# ---------------------------------------------------------------- Budget 0


@pytest.fixture
def free_costs(db: Database, clock: FrozenClock) -> CostTracker:
    return CostTracker(db, BudgetConfig(daily_limit_usd=0, monthly_limit_usd=0), clock=clock)


async def test_budget_zero_disables_paid_models(
    router_config: RouterConfig, fake_provider: FakeProvider, free_costs: CostTracker
) -> None:
    status = free_costs.status()
    assert status.paid_disabled and status.exceeded and not status.warning
    router = ModelRouter(router_config, {"fake": fake_provider}, free_costs)
    with pytest.raises(BudgetExceededError, match="Kostenlos-Modus") as info:
        await router.complete(HELLO)
    assert info.value.status.paid_disabled
    assert fake_provider.requests == []


async def test_budget_zero_still_allows_free_models(
    router_config: RouterConfig, fake_provider: FakeProvider, free_costs: CostTracker
) -> None:
    router = ModelRouter(router_config, {"fake": fake_provider}, free_costs)
    response = await router.complete(HELLO, task=TaskKind.SIMPLE)
    assert response.model == "fake-free"
    assert response.cost_usd == 0.0
    assert [r.model for r in fake_provider.requests] == ["fake-free"]


async def test_budget_zero_allows_subscription_model_without_prices(
    fake_provider: FakeProvider, free_costs: CostTracker
) -> None:
    models = [dict(m) for m in TEST_ROUTER["models"]]  # type: ignore[union-attr]
    models.append({"id": "fake-abo", "provider": "fake", "subscription": True, "trusted": True})
    router = ModelRouter(
        _config(models=models, routes={"chat": ["fake-big", "fake-abo"]}),
        {"fake": fake_provider},
        free_costs,
    )
    response = await router.complete(HELLO)
    assert response.model == "fake-abo"
    assert response.router_fallbacks == []  # fake-big wurde nicht versucht, nur übersprungen


async def test_budget_block_after_failure_reports_no_model(
    fake_provider: FakeProvider, free_costs: CostTracker
) -> None:
    router = ModelRouter(
        _config(routes={"chat": ["fake-free", "fake-big"]}), {"fake": fake_provider}, free_costs
    )
    fake_provider.behaviours["fake-free"] = ProviderUnavailableError("aus", provider="fake")
    with pytest.raises(NoModelAvailableError) as info:
        await router.complete(HELLO)
    assert "fake-big: Budget erreicht" in info.value.reasons
    assert [r.model for r in fake_provider.requests] == ["fake-free"]


# ---------------------------------------------------------------- unblock & replace_model


async def test_unblock_clears_blocks_and_pauses(
    router: ModelRouter,
    fake_provider: FakeProvider,
    fake_time: FakeTime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refreshed: list[str] = []
    monkeypatch.setattr(fake_provider, "refresh", lambda: refreshed.append("fake"))
    fake_provider.behaviours["fake-big"] = ProviderAuthError("Key ungültig", provider="fake")
    fake_provider.behaviours["fake-free"] = ProviderUnavailableError(
        "Limit", provider="fake", retry_after_s=3600
    )
    with pytest.raises(NoModelAvailableError):
        await router.complete(HELLO)
    with pytest.raises(NoModelAvailableError):
        await router.complete(HELLO, pinned_model="fake-free")
    fake_provider.requests.clear()

    # Ohne Sperre erreicht die Anfrage fake-free: der Fehler kommt bei fester Modellwahl
    # direkt durch und das Modell pausiert für die gemeldete Wartezeit.
    router._blocked_providers.clear()
    with pytest.raises(ProviderUnavailableError, match="Limit"):
        await router.complete(HELLO, pinned_model="fake-free")
    with pytest.raises(NoModelAvailableError, match="Kontingent"):
        await router.complete(HELLO, pinned_model="fake-free")
    fake_provider.behaviours["fake-big"] = ProviderAuthError("Key ungültig", provider="fake")
    with pytest.raises(NoModelAvailableError):
        await router.complete(HELLO)
    small = router.model("fake-small")
    assert small is not None
    assert "gesperrt" in (router.model_unavailable_reason(small) or "")

    fake_provider.behaviours.clear()
    router.unblock()
    assert refreshed == ["fake"]
    assert router.cooldown_remaining("fake-free") == 0.0
    assert router.model_unavailable_reason(small) is None
    assert (await router.complete(HELLO)).model == "fake-big"
    assert (await router.complete(HELLO, pinned_model="fake-free")).model == "fake-free"


async def test_replace_model_updates_catalog_and_request(
    router: ModelRouter, fake_provider: FakeProvider
) -> None:
    free = router.model("fake-free")
    assert free is not None and free.target == "fake-free"
    resolved = free.model_copy(update={"provider_model": "qwen3:8b", "options": {"num_ctx": 4096}})
    router.replace_model(resolved)
    assert router.model("fake-free") is resolved
    assert [m.id for m in router.models()] == ["fake-big", "fake-small", "fake-free", "fake-pricey"]
    await router.complete(HELLO, pinned_model="fake-free")
    [request] = fake_provider.requests
    assert request.provider_model == "qwen3:8b"
    assert request.target_model == "qwen3:8b"
    assert request.model == "fake-free"
    assert request.options == {"num_ctx": 4096}


async def test_replace_model_can_disable_and_enable(
    router: ModelRouter, fake_provider: FakeProvider
) -> None:
    big = router.model("fake-big")
    assert big is not None
    router.replace_model(big.model_copy(update={"enabled": False}))
    response = await router.complete(HELLO)
    assert response.model == "fake-small"
    assert response.router_fallbacks == []  # übersprungen, nicht fehlgeschlagen
    router.replace_model(big)
    assert (await router.complete(HELLO)).model == "fake-big"


async def test_served_provider_model_is_priced_via_target(
    router: ModelRouter, fake_provider: FakeProvider, costs: CostTracker
) -> None:
    small = router.model("fake-small")
    assert small is not None
    router.replace_model(small.model_copy(update={"provider_model": "small-v2"}))
    # Anbieter meldet den Modellnamen beim Anbieter statt der Katalog-ID.
    fake_provider.served_model["fake-big"] = "small-v2"
    response = await router.complete(HELLO)
    assert response.cost_usd == pytest.approx((1000 * 1.0 + 500 * 5.0) / 1e6)
    assert [r["model"] for r in _rows(costs)] == ["fake-small"]
