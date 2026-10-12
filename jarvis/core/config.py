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

PermissionLevel = Literal["auto", "confirm", "forbidden"]


class ConfigError(Exception):
    """Die Konfiguration fehlt oder ist ungültig."""


class _Section(BaseModel):
    """Basis für alle Abschnitte: unbekannte Schlüssel sind ein Fehler."""

    model_config = ConfigDict(extra="forbid")


class AssistantConfig(_Section):
    name: str = "JARVIS"
    user_name: str = ""
    #: Standardort für Wetter & Co., z. B. "Berlin".
    home_location: str = ""
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
    daily_limit_usd: float = Field(0.0, ge=0)
    monthly_limit_usd: float = Field(0.0, ge=0)
    warn_at_ratio: float = Field(0.8, gt=0, le=1)


class ConversationConfig(_Section):
    max_messages: int = Field(40, ge=2)
    max_chars: int = Field(100_000, ge=1_000)


class UIConfig(_Section):
    show_usage_line: bool = True
    save_input_history: bool = True


class ProviderConfig(_Section):
    """Einstellungen eines KI-Anbieters (z. B. ``gemini``)."""

    enabled: bool = True
    #: Plugin, das diesen Eintrag bedient (Standard: der Name des Eintrags).
    #: Erlaubt mehrere Einträge desselben Typs, z. B. zwei OpenAI-kompatible Dienste.
    type: str | None = None
    api_key_name: str | None = None
    base_url: str | None = None
    timeout_s: float = Field(300.0, gt=0)
    max_retries: int = Field(2, ge=0, le=10)
    options: dict[str, Any] = Field(default_factory=dict)


class ModelConfig(_Section):
    """Ein Eintrag im Modellkatalog inklusive Preisen (USD pro 1 Mio. Tokens)."""

    id: str
    provider: str
    #: Modellname beim Anbieter. Platzhalter ``@hardware.general|coder|fast``
    #: werden beim Start durch die Hardware-Empfehlung ersetzt.
    provider_model: str | None = None
    display_name: str | None = None
    enabled: bool = True
    local: bool = False
    #: Darf private Daten (E-Mails, Kalender, Dateien) sehen?
    trusted: bool = False
    #: Kosten über ein Abo abgedeckt (z. B. Claude Pro)?
    subscription: bool = False
    supports_tools: bool = False
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
    supports_effort: bool = False
    refusal_fallback: bool = False
    options: dict[str, Any] = Field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.display_name or self.id

    @property
    def target(self) -> str:
        return self.provider_model or self.id

    @property
    def is_paid(self) -> bool:
        return self.input_cost_per_mtok > 0 or self.output_cost_per_mtok > 0


class RouteTarget(_Section):
    """Ein Glied in der Fallback-Kette einer Route."""

    model: str
    effort: Effort | None = None
    max_tokens: int | None = Field(None, gt=0)


class RouterConfig(_Section):
    default_max_tokens: int = Field(8_000, gt=0)
    provider_modules: list[str] = Field(default_factory=list)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    models: list[ModelConfig] = Field(default_factory=list)
    routes: dict[TaskKind, list[RouteTarget]] = Field(default_factory=dict)

    @field_validator("routes", mode="before")
    @classmethod
    def _accept_plain_model_ids(cls, value: Any) -> Any:
        """Erlaubt in Routen auch die Kurzform ``- gemini-flash``."""
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


class SecurityConfig(_Section):
    """Berechtigungen. Diese Werte darf die Selbstverbesserung nie ändern."""

    #: Wie lange JARVIS auf deine Bestätigung wartet (danach: abgelehnt).
    confirm_timeout_s: float = Field(600, gt=0)
    #: Abweichende Stufen je Werkzeug, z. B. ``{app_open: confirm}``.
    tool_levels: dict[str, PermissionLevel] = Field(default_factory=dict)
    #: Ordner, in die JARVIS ohne Rückfrage neue Dateien schreiben darf.
    write_allowed_dirs: list[str] = Field(
        default_factory=lambda: ["@workspace", "~/Documents/JARVIS"]
    )
    #: Hier darf JARVIS nie schreiben oder löschen.
    protected_dirs: list[str] = Field(
        default_factory=lambda: [
            "C:/Windows",
            "C:/Program Files",
            "C:/Program Files (x86)",
            "C:/ProgramData",
        ]
    )
    #: Dateien, die JARVIS nie lesen darf (Keys, Tokens).
    secret_file_patterns: list[str] = Field(
        default_factory=lambda: [
            ".env",
            "*.pem",
            "*.key",
            "id_rsa*",
            "*credentials*.json",
            "*token*.json",
        ]
    )


class AgentConfig(_Section):
    max_steps: int = Field(12, ge=1, le=50)
    tool_result_max_chars: int = Field(12_000, ge=500)


class MemoryConfig(_Section):
    enabled: bool = True
    #: auto = lokales Ollama-Embedding, falls vorhanden, sonst Volltextsuche.
    embedder: Literal["auto", "ollama", "gemini", "none"] = "auto"
    max_context_facts: int = Field(8, ge=0, le=50)


class WebConfig(_Section):
    host: str = "127.0.0.1"
    port: int = Field(8765, ge=1024, le=65535)
    open_on_start: bool = True


class VoiceConfig(_Section):
    enabled: bool = True
    wake_word: str = "hey_jarvis"
    wake_threshold: float = Field(0.5, gt=0, lt=1)
    #: auto = Empfehlung aus der Hardware-Erkennung.
    stt_model: str = "auto"
    stt_device: Literal["auto", "cuda", "cpu"] = "auto"
    language: str = "de"
    tts_engine: Literal["edge", "sapi", "off"] = "edge"
    tts_voice: str = "de-DE-ConradNeural"
    #: Nach einer Antwort so lange ohne Wake-Word weiterzuhören.
    follow_up_seconds: float = Field(6.0, ge=0, le=30)
    max_record_seconds: float = Field(20.0, ge=3, le=120)
    input_device: str | int | None = None


class HotkeysConfig(_Section):
    open_window: str = "<ctrl>+<alt>+j"
    push_to_talk: str = "<ctrl>+<alt>+<space>"
    killswitch: str = "<ctrl>+<alt>+<shift>+x"


class DesktopConfig(_Section):
    tray: bool = True
    hotkeys: bool = True
    notifications: bool = True


class ProjectsConfig(_Section):
    enabled: bool = True
    max_parallel: int = Field(1, ge=1, le=4)
    max_task_attempts: int = Field(3, ge=1, le=10)
    #: Obergrenze für Arbeitsschritte pro Tag (schützt die kostenlosen Kontingente).
    max_tasks_per_day: int = Field(60, ge=1)
    pause_between_tasks_s: float = Field(10.0, ge=0)
    daily_report_time: str = "20:00"


class SelfImproveConfig(_Section):
    enabled: bool = True
    #: Claude-Code-Modell für Programmieraufgaben (Alias: sonnet, opus, haiku).
    claude_model: str = "sonnet"
    #: never = immer fragen; plugins_only = reine Plugin-Änderungen automatisch übernehmen.
    auto_merge: Literal["never", "plugins_only", "always"] = "never"
    max_fix_rounds: int = Field(2, ge=0, le=5)
    max_turns: int = Field(150, ge=10)
    #: Wie oft JARVIS selbst nach Verbesserungen sucht.
    self_check: Literal["off", "daily", "weekly"] = "weekly"
    self_check_time: str = "03:00"


class GoogleConfig(_Section):
    #: Konto für den Kalender (E-Mail-Adresse). Leer = erstes verbundenes Konto.
    calendar_account: str = ""


class ImagesConfig(_Section):
    generator: Literal["pollinations", "off"] = "pollinations"
    pollinations_model: str = ""
    default_width: int = Field(1024, ge=256, le=2048)
    default_height: int = Field(1024, ge=256, le=2048)


class PluginsConfig(_Section):
    #: Werkzeuge, die deaktiviert sein sollen (Namen wie ``powershell_run``).
    disabled: list[str] = Field(default_factory=list)


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
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    hotkeys: HotkeysConfig = Field(default_factory=HotkeysConfig)
    desktop: DesktopConfig = Field(default_factory=DesktopConfig)
    projects: ProjectsConfig = Field(default_factory=ProjectsConfig)
    self_improve: SelfImproveConfig = Field(default_factory=SelfImproveConfig)
    google: GoogleConfig = Field(default_factory=GoogleConfig)
    images: ImagesConfig = Field(default_factory=ImagesConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)


#: Abschnitte, die die Selbstverbesserung nicht verändern darf.
PROTECTED_CONFIG_SECTIONS: tuple[str, ...] = ("security", "budget", "self_improve", "secrets")


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


def read_yaml(path: Path) -> dict[str, Any]:
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

    data = read_yaml(config_path)
    sources = [config_path]
    local_path = config_path.with_name(LOCAL_CONFIG_NAME)
    if local_path.is_file() and local_path != config_path:
        data = deep_merge(data, read_yaml(local_path))
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


def update_local_config(loaded: LoadedConfig, changes: dict[str, Any]) -> Path:
    """Schreibt Änderungen (z. B. aus dem Dashboard) in ``config.local.yaml``.

    Geschützte Abschnitte lassen sich so ebenfalls ändern – das ist gewollt,
    denn diese Funktion wird nur von deinen Klicks im Dashboard ausgelöst,
    nie von der Selbstverbesserung.
    """
    target = loaded.path.with_name(LOCAL_CONFIG_NAME)
    current = read_yaml(target) if target.is_file() else {}
    merged = deep_merge(current, changes)
    # Vor dem Schreiben validieren, damit eine kaputte Datei JARVIS nicht lahmlegt.
    JarvisConfig.model_validate(deep_merge(read_yaml(loaded.path), merged))
    target.write_text(yaml.safe_dump(merged, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return target
