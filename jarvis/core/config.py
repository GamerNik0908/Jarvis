"""Konfiguration: lädt ``config.yaml`` (+ optional ``config.local.yaml``) und validiert sie.

Alle Einstellungen werden mit pydantic geprüft. Tippfehler in Schlüsselnamen
führen zu einer verständlichen Fehlermeldung statt zu stillem Fehlverhalten.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import platformdirs
import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from jarvis.core.types import Effort, LogLevel, TaskKind

#: Wurzelordner des Projekts (enthält config.yaml und .env).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_NAME = "config.yaml"
LOCAL_CONFIG_NAME = "config.local.yaml"
ENV_CONFIG_PATH = "JARVIS_CONFIG"
ENV_DATA_DIR = "JARVIS_DATA_DIR"
APP_DIR_NAME = "Jarvis"


class ConfigError(Exception):
    """Die Konfiguration fehlt oder ist ungültig."""


class _Section(BaseModel):
    """Basis für alle Abschnitte: unbekannte Schlüssel sind ein Fehler."""

    model_config = ConfigDict(extra="forbid")


class AssistantConfig(_Section):
    name: str = "JARVIS"
    user_name: str = ""
    system_prompt: str = "Du bist {name}, ein hilfreicher persönlicher Assistent."


class PathsConfig(_Section):
    data_dir: str = "auto"

    def resolve_data_dir(self, base_dir: Path) -> Path:
        """Liefert den absoluten Datenordner.

        Die Umgebungsvariable ``JARVIS_DATA_DIR`` hat Vorrang vor der Konfiguration.
        ``auto`` bedeutet ``%LOCALAPPDATA%\\Jarvis`` unter Windows.
        """
        raw = os.environ.get(ENV_DATA_DIR, "").strip() or self.data_dir.strip()
        if raw.lower() == "auto":
            return Path(platformdirs.user_data_dir(APP_DIR_NAME, appauthor=False, roaming=False))
        path = Path(os.path.expandvars(raw)).expanduser()
        if not path.is_absolute():
            path = base_dir / path
        return path.resolve()


class LoggingConfig(_Section):
    file_level: LogLevel = "INFO"
    console_level: LogLevel = "WARNING"
    max_bytes: int = Field(5_000_000, ge=10_000)
    backup_count: int = Field(5, ge=0)


class SecretsConfig(_Section):
    use_keyring: bool = True
    keyring_service: str = "JARVIS"


class BudgetConfig(_Section):
    daily_limit_usd: float = Field(2.0, ge=0)
    monthly_limit_usd: float = Field(30.0, ge=0)
    warn_at_ratio: float = Field(0.8, gt=0, le=1)


class ConversationConfig(_Section):
    max_messages: int = Field(40, ge=2)
    max_chars: int = Field(100_000, ge=1_000)


class UIConfig(_Section):
    show_usage_line: bool = True
    save_input_history: bool = True


class ProviderConfig(_Section):
    """Einstellungen eines KI-Anbieters (z. B. ``anthropic``)."""

    enabled: bool = True
    api_key_name: str | None = None
    base_url: str | None = None
    timeout_s: float = Field(300.0, gt=0)
    max_retries: int = Field(2, ge=0, le=10)
    options: dict[str, Any] = Field(default_factory=dict)


class ModelConfig(_Section):
    """Ein Eintrag im Modellkatalog inklusive Preisen (USD pro 1 Mio. Tokens)."""

    id: str
    provider: str
    display_name: str | None = None
    enabled: bool = True
    local: bool = False
    quality: int = Field(5, ge=1, le=10)
    speed: int = Field(5, ge=1, le=10)
    context_window: int = Field(200_000, gt=0)
    max_output_tokens: int = Field(16_000, gt=0)
    input_cost_per_mtok: float = Field(0.0, ge=0)
    output_cost_per_mtok: float = Field(0.0, ge=0)
    cache_read_cost_per_mtok: float | None = Field(None, ge=0)
    cache_write_cost_per_mtok: float | None = Field(None, ge=0)
    long_context_threshold_tokens: int | None = Field(None, gt=0)
    long_input_cost_per_mtok: float | None = Field(None, ge=0)
    long_output_cost_per_mtok: float | None = Field(None, ge=0)
    supports_effort: bool = True
    refusal_fallback: bool = False

    @property
    def label(self) -> str:
        return self.display_name or self.id

    @property
    def is_paid(self) -> bool:
        return self.input_cost_per_mtok > 0 or self.output_cost_per_mtok > 0


class RouteTarget(_Section):
    """Ein Glied in der Fallback-Kette einer Route."""

    model: str
    effort: Effort | None = None
    max_tokens: int | None = Field(None, gt=0)


class RouterConfig(_Section):
    default_max_tokens: int = Field(16_000, gt=0)
    provider_modules: list[str] = Field(default_factory=list)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    models: list[ModelConfig] = Field(default_factory=list)
    routes: dict[TaskKind, list[RouteTarget]] = Field(default_factory=dict)

    @field_validator("routes", mode="before")
    @classmethod
    def _accept_plain_model_ids(cls, value: Any) -> Any:
        """Erlaubt in Routen auch die Kurzform ``- claude-opus-5-5``."""
        if not isinstance(value, dict):
            return value
        return {
            task: [{"model": t} if isinstance(t, str) else t for t in targets]
            if isinstance(targets, list)
            else targets
            for task, targets in value.items()
        }

    @model_validator(mode="after")
    def _check_references(self) -> RouterConfig:
        ids = [m.id for m in self.models]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"Modell-IDs doppelt vergeben: {', '.join(duplicates)}")
        for model in self.models:
            if model.provider not in self.providers:
                raise ValueError(
                    f"Modell '{model.id}' verweist auf unbekannten Anbieter '{model.provider}'"
                )
        known = set(ids)
        for task, targets in self.routes.items():
            if not targets:
                raise ValueError(f"Route '{task}' ist leer")
            for target in targets:
                if target.model not in known:
                    raise ValueError(
                        f"Route '{task}' verweist auf unbekanntes Modell '{target.model}'"
                    )
        if TaskKind.CHAT not in self.routes and self.models:
            raise ValueError("Route 'chat' fehlt – sie dient als Standard für alle Aufgaben")
        return self

    def model(self, model_id: str) -> ModelConfig | None:
        return next((m for m in self.models if m.id == model_id), None)


class LocalModelTier(_Section):
    """Empfehlungsstufe für lokale Modelle abhängig von der Hardware."""

    name: str
    requires_gpu: bool = False
    min_vram_gb: float = Field(0.0, ge=0)
    min_ram_gb: float = Field(0.0, ge=0)
    general: str
    coder: str
    fast: str
    whisper_model: str = "small"
    whisper_device: Literal["cuda", "cpu"] = "cpu"
    whisper_compute_type: str = "int8"


def _default_tiers() -> list[LocalModelTier]:
    return [
        LocalModelTier(
            name="minimal", general="qwen3:1.7b", coder="qwen2.5-coder:1.5b", fast="qwen3:0.6b"
        )
    ]


class HardwareConfig(_Section):
    embedding_model: str = "nomic-embed-text"
    local_model_tiers: list[LocalModelTier] = Field(default_factory=_default_tiers, min_length=1)


class JarvisConfig(_Section):
    """Gesamte Konfiguration von JARVIS."""

    assistant: AssistantConfig = Field(default_factory=AssistantConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    router: RouterConfig = Field(default_factory=RouterConfig)
    hardware: HardwareConfig = Field(default_factory=HardwareConfig)


@dataclass(frozen=True)
class LoadedConfig:
    """Validierte Konfiguration plus Herkunft."""

    config: JarvisConfig
    path: Path
    local_path: Path | None

    @property
    def base_dir(self) -> Path:
        return self.path.parent

    def data_dir(self) -> Path:
        return self.config.paths.resolve_data_dir(self.base_dir)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Führt zwei Dictionaries rekursiv zusammen; Listen und Werte werden ersetzt."""
    result = dict(base)
    for key, value in override.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = deep_merge(current, value)
        else:
            result[key] = value
    return result


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8-sig") as handle:  # BOM-tolerant (Notepad)
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML-Fehler in {path}:\n{exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Konfiguration nicht lesbar: {path} ({exc})") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} muss ein YAML-Dictionary enthalten")
    return data


def _format_validation_error(exc: ValidationError, sources: list[Path]) -> str:
    lines = [f"Ungültige Konfiguration ({', '.join(str(s) for s in sources)}):"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(Wurzel)"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)


def load_config(path: Path | str | None = None) -> LoadedConfig:
    """Lädt und validiert die Konfiguration.

    Reihenfolge der Pfadsuche: Argument > ``JARVIS_CONFIG`` > ``<Projekt>/config.yaml``.
    Liegt neben der Datei eine ``config.local.yaml``, wird sie darübergelegt.
    """
    raw_path = path or os.environ.get(ENV_CONFIG_PATH) or PROJECT_ROOT / DEFAULT_CONFIG_NAME
    config_path = Path(raw_path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(f"Konfigurationsdatei nicht gefunden: {config_path}")

    data = _read_yaml(config_path)
    sources = [config_path]
    local_path = config_path.with_name(LOCAL_CONFIG_NAME)
    if local_path.is_file() and local_path != config_path:
        data = deep_merge(data, _read_yaml(local_path))
        sources.append(local_path)

    try:
        config = JarvisConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc, sources)) from exc
    return LoadedConfig(
        config=config,
        path=config_path,
        local_path=local_path if len(sources) > 1 else None,
    )
