"""Verdrahtet alle Bausteine zu einer lauffähigen JARVIS-Instanz."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jarvis import PHASE, __version__
from jarvis.core.config import JarvisConfig, LoadedConfig, load_config
from jarvis.core.conversation import Conversation
from jarvis.core.db import Database
from jarvis.core.logging_setup import setup_logging
from jarvis.router.costs import CostTracker
from jarvis.router.registry import create_providers
from jarvis.router.router import ModelRouter
from jarvis.security.secrets import SecretStore

log = logging.getLogger(__name__)

_WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
_MONTHS = (
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
)  # fmt: skip


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


@dataclass
class JarvisApp:
    loaded: LoadedConfig
    data_dir: Path
    log_file: Path
    secrets: SecretStore
    db: Database
    costs: CostTracker
    router: ModelRouter

    @property
    def config(self) -> JarvisConfig:
        return self.loaded.config

    @classmethod
    def bootstrap(
        cls,
        config_path: Path | str | None = None,
        *,
        role: str = "chat",
        console_level: str | None = None,
    ) -> JarvisApp:
        """Lädt Konfiguration, richtet Logging/Datenbank ein und baut den Router."""
        loaded = load_config(config_path)
        config = loaded.config
        data_dir = loaded.data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        log_file = setup_logging(
            config.logging, data_dir / "logs", role=role, console_level=console_level
        )
        log.info(
            "JARVIS %s (Phase %s) startet – Konfiguration: %s", __version__, PHASE, loaded.path
        )
        if loaded.local_path:
            log.info("Lokale Überschreibungen: %s", loaded.local_path)

        secrets = make_secret_store(loaded)
        db = Database(data_dir / "jarvis.db")
        db.migrate()
        costs = CostTracker(db, config.budget)
        providers = create_providers(config.router, secrets)
        router = ModelRouter(config.router, providers, costs)
        return cls(
            loaded=loaded,
            data_dir=data_dir,
            log_file=log_file,
            secrets=secrets,
            db=db,
            costs=costs,
            router=router,
        )

    def system_prompt(self, now: datetime | None = None) -> str:
        """System-Prompt mit eingesetzten Platzhaltern.

        Bewusst nur das Datum, keine Uhrzeit: Der Prompt bleibt so den ganzen
        Tag identisch und das Prompt-Caching des Anbieters greift.
        """
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
        self.db.close()
