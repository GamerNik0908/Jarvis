from __future__ import annotations

import asyncio

import pytest

from jarvis.core.config import RouterConfig
from jarvis.core.types import TaskKind
from jarvis.router.base import ChatMessage, StreamChunk, UsageSegment
from jarvis.router.costs import CostTracker, UsageRecord
from jarvis.router.errors import (
    BudgetExceededError,
    ModelNotFoundError,
    NoModelAvailableError,
    ProviderAuthError,
    ProviderRequestError,
    ProviderUnavailableError,
)
from jarvis.router.router import ModelRouter

from .conftest import FakeProvider

HELLO = [ChatMessage(role="user", content="Hallo")]


def _rows(costs: CostTracker) -> list[dict[str, object]]:
    return [dict(r) for r in costs._db.query("SELECT * FROM model_usage ORDER BY id")]


async def _collect(router: ModelRouter, **kwargs: object) -> tuple[str, list[StreamChunk]]:
    chunks = [c async for c in router.stream(HELLO, **kwargs)]  # type: ignore[arg-type]
    return "".join(c.text for c in chunks), chunks


async def test_uses_first_model_of_route_and_records_cost(
    router: ModelRouter, fake_provider: FakeProvider, costs: CostTracker
) -> None:
    text, chunks = await _collect(router, system="sys")
    response = chunks[-1].response
    assert text == "Hallo Welt"
    assert response is not None
    assert response.model == "fake-big"
    assert response.task is TaskKind.CHAT
    # 1000 × $3/MTok + 500 × $15/MTok
    assert response.cost_usd == pytest.approx(0.003 + 0.0075)
    request = fake_provider.requests[0]
    assert request.effort == "low"
    assert request.system == "sys"
    assert request.max_tokens == 800  # default 1000, aber Modell-Limit 800
    rows = _rows(costs)
    assert len(rows) == 1 and rows[0]["success"] == 1
    assert costs.spent_today() == pytest.approx(0.0105)


async def test_route_specific_max_tokens(router: ModelRouter, fake_provider: FakeProvider) -> None:
    fake_provider.behaviours["fake-big"] = ["ok"]
    await router.complete(HELLO, task=TaskKind.CODE)
    assert fake_provider.requests[0].max_tokens == 800  # Route will 5000, Modell kann 800
    assert fake_provider.requests[0].effort == "high"


async def test_task_without_route_uses_chat_route(router: ModelRouter) -> None:
    response = await router.complete(HELLO, task=TaskKind.PLANNING)
    assert response.model == "fake-big"


async def test_fallback_before_any_output(
    router: ModelRouter, fake_provider: FakeProvider, costs: CostTracker
) -> None:
    fake_provider.behaviours["fake-big"] = ProviderUnavailableError("überlastet", provider="fake")
    response = await router.complete(HELLO)
    assert response.model == "fake-small"
    assert response.router_fallbacks == ["fake-big"]
    rows = _rows(costs)
    assert [r["success"] for r in rows] == [0, 1]
    assert "ProviderUnavailableError" in str(rows[0]["error"])


async def test_model_not_found_falls_back(router: ModelRouter, fake_provider: FakeProvider) -> None:
    fake_provider.behaviours["fake-big"] = ModelNotFoundError("weg", provider="fake")
    assert (await router.complete(HELLO)).model == "fake-small"


async def test_no_fallback_after_partial_output(
    router: ModelRouter, fake_provider: FakeProvider
) -> None:
    fake_provider.behaviours["fake-big"] = ["Teil"]
    fake_provider.fail_after["fake-big"] = ProviderUnavailableError("Abbruch", provider="fake")
    received: list[str] = []
    with pytest.raises(ProviderUnavailableError):
        async for chunk in router.stream(HELLO):
            received.append(chunk.text)
    assert received == ["Teil"]
    assert [r.model for r in fake_provider.requests] == ["fake-big"]


async def test_invalid_request_is_not_retried(
    router: ModelRouter, fake_provider: FakeProvider
) -> None:
    fake_provider.behaviours["fake-big"] = ProviderRequestError("400", provider="fake")
    with pytest.raises(ProviderRequestError):
        await router.complete(HELLO)
    assert len(fake_provider.requests) == 1


async def test_auth_error_blocks_provider_for_session(
    router: ModelRouter, fake_provider: FakeProvider
) -> None:
    fake_provider.behaviours["fake-big"] = ProviderAuthError("Key ungültig", provider="fake")
    with pytest.raises(NoModelAvailableError) as info:
        await router.complete(HELLO)
    # fake-small gehört zum selben Anbieter und wird nicht mehr versucht.
    assert [r.model for r in fake_provider.requests] == ["fake-big"]
    assert any("Key ungültig" in reason for reason in info.value.reasons)
    model = router.model("fake-small")
    assert model is not None
    assert "gesperrt" in (router.model_unavailable_reason(model) or "")


async def test_pinned_model_has_no_fallback(
    router: ModelRouter, fake_provider: FakeProvider
) -> None:
    fake_provider.behaviours["fake-small"] = ProviderUnavailableError("down", provider="fake")
    with pytest.raises(ProviderUnavailableError):
        await router.complete(HELLO, pinned_model="fake-small")
    assert [r.model for r in fake_provider.requests] == ["fake-small"]


async def test_pinned_model_keeps_route_effort(
    router: ModelRouter, fake_provider: FakeProvider
) -> None:
    await router.complete(HELLO, pinned_model="fake-big")
    assert fake_provider.requests[0].effort == "low"


async def test_unknown_or_disabled_pinned_model(router: ModelRouter) -> None:
    with pytest.raises(NoModelAvailableError, match="unbekanntes Modell"):
        await router.complete(HELLO, pinned_model="gibt-es-nicht")
    with pytest.raises(NoModelAvailableError, match="deaktiviert"):
        await router.complete(HELLO, pinned_model="fake-pricey")


async def test_budget_blocks_paid_models_before_calling(
    router: ModelRouter, fake_provider: FakeProvider, costs: CostTracker
) -> None:
    costs.record(UsageRecord(provider="fake", model="x", task="chat", cost_usd=1.0))
    with pytest.raises(BudgetExceededError):
        await router.complete(HELLO)
    assert fake_provider.requests == []


async def test_budget_still_allows_free_models(
    router: ModelRouter, fake_provider: FakeProvider, costs: CostTracker
) -> None:
    costs.record(UsageRecord(provider="fake", model="x", task="chat", cost_usd=1.0))
    response = await router.complete(HELLO, task=TaskKind.SIMPLE)
    assert response.model == "fake-free"
    assert response.cost_usd == 0


async def test_unconfigured_provider_reports_reason(
    router_config: RouterConfig, costs: CostTracker
) -> None:
    router = ModelRouter(router_config, {}, costs)
    with pytest.raises(NoModelAvailableError) as info:
        await router.complete(HELLO)
    assert "nicht geladen" in str(info.value)


async def test_provider_side_fallback_is_priced_per_model(
    router: ModelRouter, fake_provider: FakeProvider, costs: CostTracker
) -> None:
    # Anbieter hat intern von fake-big auf fake-small gewechselt (z. B. Refusal-Fallback).
    fake_provider.served_model["fake-big"] = "fake-small"
    fake_provider.extra_segments["fake-big"] = [
        UsageSegment(model="fake-big", input_tokens=1000, output_tokens=0)
    ]
    response = await router.complete(HELLO)
    expected = 1000 * 3.0 / 1e6 + (1000 * 1.0 + 500 * 5.0) / 1e6
    assert response.cost_usd == pytest.approx(expected)
    assert [r["model"] for r in _rows(costs)] == ["fake-big", "fake-small"]


async def test_unknown_served_model_is_priced_as_requested(
    router: ModelRouter, fake_provider: FakeProvider
) -> None:
    fake_provider.served_model["fake-big"] = "mystery-model"
    response = await router.complete(HELLO)
    assert response.cost_usd == pytest.approx(0.0105)


async def test_cancellation_records_estimated_usage(
    router: ModelRouter, fake_provider: FakeProvider, costs: CostTracker
) -> None:
    fake_provider.behaviours["fake-big"] = ["a" * 400, "b" * 400, "c" * 400]
    fake_provider.delays["fake-big"] = 0.05
    first_chunk = asyncio.Event()

    async def consume() -> None:
        async for _ in router.stream(HELLO):
            first_chunk.set()

    task = asyncio.create_task(consume())
    await first_chunk.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = _rows(costs)
    assert len(rows) == 1
    assert rows[0]["estimated"] == 1 and rows[0]["success"] == 0
    assert int(rows[0]["output_tokens"]) >= 100  # type: ignore[call-overload]
    assert float(rows[0]["cost_usd"]) > 0  # type: ignore[arg-type]


async def test_consumer_break_after_final_chunk_is_not_counted_twice(
    router: ModelRouter, costs: CostTracker
) -> None:
    stream = router.stream(HELLO)
    async for chunk in stream:
        if chunk.response is not None:
            break
    await stream.aclose()
    assert len(_rows(costs)) == 1
