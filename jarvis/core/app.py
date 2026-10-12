"""Verdrahtet alle Bausteine zu einer lauffähigen JARVIS-Instanz."""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

import httpx

from jarvis import __version__
from jarvis.core.config import PROJECT_ROOT, JarvisConfig, LoadedConfig, ModelConfig, load_config
from jarvis.core.conversation import Conversation
from jarvis.core.db import Database
from jarvis.core.events import EventBus
from jarvis.core.hardware import (
    HardwareInfo,
    ModelRecommendation,
    detect_hardware,
    recommend_local_models,
    save_hardware_report,
)
from jarvis.core.logging_setup import setup_logging
from jarvis.core.stores import CardStore, ImageStore, ReminderStore
from jarvis.integrations.google import GoogleAccounts
from jarvis.memory.conversations import ConversationStore
from jarvis.memory.store import Embedder, MemoryStore
from jarvis.plugins.base import ToolRegistry, load_plugins
from jarvis.plugins.executor import ToolExecutor
from jarvis.router.costs import CostTracker
from jarvis.router.providers.claude_code_provider import ClaudeCodeProvider
from jarvis.router.providers.gemini_provider import GeminiProvider
from jarvis.router.providers.ollama_provider import OllamaProvider
from jarvis.router.registry import create_providers
from jarvis.router.router import ModelRouter
from jarvis.security.audit import AuditLog
from jarvis.security.killswitch import KillSwitch
from jarvis.security.paths import PathPolicy
from jarvis.security.permissions import ConfirmationBroker
from jarvis.security.secrets import SecretStore

if TYPE_CHECKING:
    from jarvis.core.agent import Agent
    from jarvis.projects.manager import ProjectManager
    from jarvis.self_improve.manager import ImprovementManager
    from jarvis.voice.service import VoiceService

log = logging.getLogger(__name__)

_P = TypeVar("_P")

_WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
_MONTHS = (
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
)  # fmt: skip
_HARDWARE_MAX_AGE_S = 24 * 3600
USER_AGENT = f"JARVIS/{__version__} (personal assistant)"  # nur ASCII – HTTP-Header!


def german_date(value: datetime) -> str:
    """Datum unabhängig von der Windows-Ländereinstellung, z. B. 'Montag, 12. Oktober 2026'."""
    return f"{_WEEKDAYS[value.weekday()]}, {value.day}. {_MONTHS[value.month - 1]} {value.year}"


class _KeepUnknown(dict[str, str]):
    """Unbekannte Platzhalter im System-Prompt bleiben stehen statt Fehler zu werfen."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def make_secret_store(loaded: LoadedConfig) -> SecretStore:
    return SecretStore(
        env_file=loaded.base_dir / ".env",
        use_keyring=loaded.config.secrets.use_keyring,
        keyring_service=loaded.config.secrets.keyring_service,
    )


def resolve_hardware_models(
    models: Sequence[ModelConfig], recommendation: ModelRecommendation | None
) -> list[ModelConfig]:
    """Ersetzt ``@hardware.general|coder|fast|embedding`` durch die empfohlenen Modelle."""
    if recommendation is None:
        return list(models)
    mapping = {
        "@hardware.general": recommendation.general,
        "@hardware.coder": recommendation.coder,
        "@hardware.fast": recommendation.fast,
        "@hardware.embedding": recommendation.embedding,
    }
    resolved = []
    for model in models:
        target = model.provider_model or ""
        if target in mapping:
            model = model.model_copy(update={"provider_model": mapping[target]})
        resolved.append(model)
    return resolved


def load_or_detect_hardware(
    config: JarvisConfig, data_dir: Path
) -> tuple[HardwareInfo | None, ModelRecommendation | None]:
    report = data_dir / "hardware.json"
    hardware: HardwareInfo | None = None
    try:
        if report.is_file() and time.time() - report.stat().st_mtime < _HARDWARE_MAX_AGE_S:
            from jarvis.core.hardware import GPUInfo

            data = json.loads(report.read_text(encoding="utf-8"))["hardware"]
            data["gpus"] = [GPUInfo(**g) for g in data.get("gpus", [])]
            hardware = HardwareInfo(**data)
    except (OSError, KeyError, TypeError, ValueError):
        hardware = None
    if hardware is None:
        try:
            hardware = detect_hardware(data_dir)
        except Exception:
            log.exception("Hardware-Erkennung fehlgeschlagen")
            return None, None
    recommendation = recommend_local_models(
        hardware, config.hardware.local_model_tiers, embedding_model=config.hardware.embedding_model
    )
    with contextlib.suppress(OSError):
        save_hardware_report(hardware, recommendation, report)
    return hardware, recommendation


@dataclass
class JarvisApp:
    loaded: LoadedConfig
    data_dir: Path
    log_file: Path
    secrets: SecretStore
    db: Database
    costs: CostTracker
    router: ModelRouter
    events: EventBus
    broker: ConfirmationBroker
    audit: AuditLog
    killswitch: KillSwitch
    tools: ToolRegistry
    executor: ToolExecutor
    memory: MemoryStore
    conversations: ConversationStore
    reminders: ReminderStore
    cards: CardStore
    images: ImageStore
    google: GoogleAccounts
    paths: PathPolicy
    workspace: Path
    hardware: HardwareInfo | None = None
    recommendation: ModelRecommendation | None = None
    projects: ProjectManager | None = None
    improvements: ImprovementManager | None = None
    voice: VoiceService | None = None
    notify_handlers: list[Callable[[str, str], None]] = field(default_factory=list)
    _http: httpx.AsyncClient | None = None
    _agent: Agent | None = None

    @property
    def config(self) -> JarvisConfig:
        return self.loaded.config

    @property
    def agent(self) -> Agent:
        if self._agent is None:
            from jarvis.core.agent import Agent

            self._agent = Agent(self)
        return self._agent

    @classmethod
    def bootstrap(
        cls,
        config_path: Path | str | None = None,
        *,
        role: str = "chat",
        console_level: str | None = None,
        detect: bool = True,
    ) -> JarvisApp:
        """Lädt Konfiguration, richtet Logging/Datenbank ein und baut alle Bausteine."""
        loaded = load_config(config_path)
        config = loaded.config
        data_dir = loaded.data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        log_file = setup_logging(
            config.logging, data_dir / "logs", role=role, console_level=console_level
        )
        log.info("JARVIS %s startet (%s) – Konfiguration: %s", __version__, role, loaded.path)
        if loaded.local_path:
            log.info("Lokale Überschreibungen: %s", loaded.local_path)

        secrets = make_secret_store(loaded)
        db = Database(data_dir / "jarvis.db")
        db.migrate()
        costs = CostTracker(db, config.budget)

        hardware, recommendation = (
            load_or_detect_hardware(config, data_dir) if detect else (None, None)
        )
        router_config = config.router.model_copy(
            update={"models": resolve_hardware_models(config.router.models, recommendation)}
        )
        providers = create_providers(router_config, secrets)
        for provider in providers.values():
            if isinstance(provider, ClaudeCodeProvider):
                provider.configure(workdir=data_dir / "claude")
        router = ModelRouter(router_config, providers, costs)

        events = EventBus()
        broker = ConfirmationBroker(events, timeout_s=config.security.confirm_timeout_s)
        audit = AuditLog(db)
        killswitch = KillSwitch(events)
        registry = load_plugins(ToolRegistry(), disabled=config.plugins.disabled)
        executor = ToolExecutor(
            registry,
            broker=broker,
            audit=audit,
            killswitch=killswitch,
            events=events,
            security=config.security,
            agent=config.agent,
        )
        workspace = data_dir / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        app = cls(
            loaded=loaded,
            data_dir=data_dir,
            log_file=log_file,
            secrets=secrets,
            db=db,
            costs=costs,
            router=router,
            events=events,
            broker=broker,
            audit=audit,
            killswitch=killswitch,
            tools=registry,
            executor=executor,
            memory=MemoryStore(db, None),
            conversations=ConversationStore(db),
            reminders=ReminderStore(db),
            cards=CardStore(db),
            images=ImageStore(db, data_dir / "images"),
            google=GoogleAccounts(
                data_dir / "google", calendar_account=config.google.calendar_account
            ),
            paths=PathPolicy(
                config.security, workspace=workspace, data_dir=data_dir, project_root=PROJECT_ROOT
            ),
            workspace=workspace,
            hardware=hardware,
            recommendation=recommendation,
        )
        app.memory.embedder = app._make_embedder()
        return app

    # ------------------------------------------------------------------ Dienste

    def provider_of_type(self, cls: type[_P]) -> _P | None:
        return next((p for p in self.router.providers.values() if isinstance(p, cls)), None)

    def _make_embedder(self) -> Embedder | None:
        choice = self.config.memory.embedder
        if choice == "none":
            return None
        ollama = self.provider_of_type(OllamaProvider)
        gemini = self.provider_of_type(GeminiProvider)
        model = (
            self.recommendation.embedding
            if self.recommendation
            else self.config.hardware.embedding_model
        )
        if choice in ("auto", "ollama") and ollama is not None and ollama.config.enabled:

            async def embed_local(texts: Sequence[str]) -> list[list[float]]:
                installed = ollama.installed_models
                if ollama.unavailable_reason() or (
                    installed
                    and not any(name.split(":")[0] == model.split(":")[0] for name in installed)
                ):
                    raise RuntimeError("lokales Embedding-Modell nicht verfügbar")
                return await ollama.embed(texts, model)

            return Embedder(name=f"ollama:{model}", embed=embed_local)
        if choice == "gemini" and gemini is not None:

            async def embed_gemini(texts: Sequence[str]) -> list[list[float]]:
                return await gemini.embed(texts)

            return Embedder(name="gemini:gemini-embedding-2", embed=embed_gemini)
        return None

    def http(self) -> httpx.AsyncClient:
        """Gemeinsamer HTTP-Client für Werkzeuge."""
        if self._http is None:
            self._http = httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT},
                timeout=httpx.Timeout(30, connect=10),
                follow_redirects=True,
            )
        return self._http

    async def startup_probes(self) -> None:
        """Prüft im Hintergrund, welche Anbieter wirklich bereitstehen."""
        for provider in self.router.providers.values():
            probe = getattr(provider, "probe", None)
            if probe is None or not provider.config.enabled:
                continue
            try:
                await probe()
            except Exception:
                log.exception("Prüfung von %s fehlgeschlagen", provider.instance_name)

    def notify(self, title: str, message: str, *, speak: bool = False, level: str = "info") -> None:
        """Benachrichtigung an Dashboard, Tray (Windows-Toast) und optional per Sprache."""
        self.events.publish("notification", {"title": title, "message": message, "level": level})
        if self.config.desktop.notifications:
            for handler in list(self.notify_handlers):
                try:
                    handler(title, message)
                except Exception:
                    log.debug("Benachrichtigung fehlgeschlagen", exc_info=True)
        if speak and self.voice is not None:
            self.voice.say_soon(message)

    def system_prompt(self, now: datetime | None = None) -> str:
        """Grund-System-Prompt mit eingesetzten Platzhaltern."""
        assistant = self.loaded.config.assistant
        values = _KeepUnknown(
            name=assistant.name,
            user=assistant.user_name.strip() or "deinem Benutzer",
            date=german_date(now or datetime.now()),
        )
        return assistant.system_prompt.format_map(values).strip()

    def new_conversation(self) -> Conversation:
        conv = self.loaded.config.conversation
        return Conversation(max_messages=conv.max_messages, max_chars=conv.max_chars)

    async def aclose(self) -> None:
        await self.router.aclose()
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        self.db.close()
