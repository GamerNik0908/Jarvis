"""Gemeinsame Test-Hilfen: ein steuerbarer Fake-Anbieter und eine Test-Konfiguration."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable, Iterator
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from jarvis.core.config import BudgetConfig, JarvisConfig, ProviderConfig, RouterConfig
from jarvis.core.db import Database
from jarvis.router.base import ChatRequest, ChatResponse, LLMProvider, StreamChunk, UsageSegment
from jarvis.router.costs import CostTracker
from jarvis.router.registry import register_provider
from jarvis.router.router import ModelRouter
from jarvis.security.redaction import clear_registered_secrets
from jarvis.security.secrets import SecretStore

Behaviour = Callable[[ChatRequest], list[str] | Exception]


@register_provider
class FakeProvider(LLMProvider):
    """Antwortet mit vorgegebenen Text-Chunks oder wirft vorgegebene Fehler.

    ``behaviours`` bildet Modell-ID → Verhalten ab. Ein Verhalten ist entweder
    eine Liste von Chunks oder eine Exception. Mit ``fail_after`` wird die
    Exception erst nach den Chunks geworfen.
    """

    name = "fake"
    default_api_key_name = None

    def __init__(
        self, config: ProviderConfig, secrets: SecretStore, *, instance_name: str | None = None
    ) -> None:
        super().__init__(config, secrets, instance_name=instance_name)
        self.behaviours: dict[str, list[str] | Exception] = {}
        #: Warteschlange fertiger Antworten pro Modell (z. B. mit Tool-Aufrufen); hat Vorrang.
        self.scripted: dict[str, list[ChatResponse]] = {}
        self.fail_after: dict[str, Exception] = {}
        self.delays: dict[str, float] = {}
        self.served_model: dict[str, str] = {}
        self.extra_segments: dict[str, list[UsageSegment]] = {}
        self.requests: list[ChatRequest] = []

    async def stream(self, request: ChatRequest) -> AsyncGenerator[StreamChunk, None]:
        self.requests.append(request)
        queue = self.scripted.get(request.model)
        if queue:
            scripted = queue.pop(0)
            if scripted.text:
                yield StreamChunk(text=scripted.text)
            yield StreamChunk(response=scripted)
            return
        behaviour = self.behaviours.get(request.model, ["Hallo", " Welt"])
        if isinstance(behaviour, Exception):
            raise behaviour
        for text in behaviour:
            if request.model in self.delays:
                await asyncio.sleep(self.delays[request.model])
            yield StreamChunk(text=text)
        if request.model in self.fail_after:
            raise self.fail_after[request.model]
        served = self.served_model.get(request.model, request.model)
        segments = [
            *self.extra_segments.get(request.model, []),
            UsageSegment(model=served, input_tokens=1000, output_tokens=500),
        ]
        yield StreamChunk(
            response=ChatResponse(
                text="".join(behaviour),
                provider=self.name,
                model=served,
                requested_model=request.model,
                stop_reason="end_turn",
                usage=segments,
            )
        )


class FrozenClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


TEST_ROUTER = {
    "default_max_tokens": 1000,
    "providers": {"fake": {"enabled": True}},
    "models": [
        {
            "id": "fake-big",
            "provider": "fake",
            "quality": 9,
            "input_cost_per_mtok": 3.0,
            "output_cost_per_mtok": 15.0,
            "max_output_tokens": 800,
            "supports_effort": True,
            "supports_tools": True,
        },
        {
            "id": "fake-small",
            "provider": "fake",
            "quality": 5,
            "input_cost_per_mtok": 1.0,
            "output_cost_per_mtok": 5.0,
            "supports_tools": True,
        },
        {"id": "fake-free", "provider": "fake", "local": True, "trusted": True},
        {
            "id": "fake-pricey",
            "provider": "fake",
            "enabled": False,
            "input_cost_per_mtok": 10.0,
            "output_cost_per_mtok": 50.0,
        },
    ],
    "routes": {
        "chat": [{"model": "fake-big", "effort": "low"}, "fake-small"],
        "simple": ["fake-small", "fake-free"],
        "code": [{"model": "fake-big", "effort": "high", "max_tokens": 5000}],
    },
}


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Keine echten Keys, kein echter Datenordner, keine registrierten Secrets aus Vortests."""
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "JARVIS_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    clear_registered_secrets()
    yield
    clear_registered_secrets()


@pytest.fixture
def router_config() -> RouterConfig:
    return RouterConfig.model_validate(TEST_ROUTER)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 10, 12, 14, 30).astimezone())


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "test.db")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def costs(db: Database, clock: FrozenClock) -> CostTracker:
    return CostTracker(db, BudgetConfig(daily_limit_usd=1.0, monthly_limit_usd=5.0), clock=clock)


@pytest.fixture
def secrets() -> SecretStore:
    return SecretStore(use_keyring=False)


@pytest.fixture
def fake_provider(secrets: SecretStore) -> FakeProvider:
    return FakeProvider(ProviderConfig(), secrets)


@pytest.fixture
def router(
    router_config: RouterConfig, fake_provider: FakeProvider, costs: CostTracker
) -> ModelRouter:
    return ModelRouter(router_config, {"fake": fake_provider}, costs)


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    """Vollständige Konfigurationsdatei mit dem Fake-Anbieter."""
    config = JarvisConfig.model_validate(
        {
            "paths": {"data_dir": str(tmp_path / "data")},
            "secrets": {"use_keyring": False},
            "memory": {"embedder": "none"},
            "budget": {"daily_limit_usd": 1.0, "monthly_limit_usd": 5.0},
            "router": TEST_ROUTER,
        }
    )
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    return path
