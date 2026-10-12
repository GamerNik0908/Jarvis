"""Gemeinsame Datentypen und die Schnittstelle für KI-Anbieter.

Ein Anbieter (Claude Code, Gemini, Ollama, …) ist ein Plugin, das
:class:`LLMProvider` implementiert. Der Router und der Agent-Loop sprechen
ausschließlich mit dieser Schnittstelle und kennen keine anbieterspezifischen
Details. Werkzeug-Aufrufe (Function Calling) werden in einem neutralen Format
beschrieben und von jedem Anbieter in sein eigenes Format übersetzt.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

from jarvis.core.config import ModelConfig, ProviderConfig
from jarvis.core.types import Effort, TaskKind
from jarvis.security.secrets import SecretStore

Role = Literal["user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolSpec:
    """Beschreibung eines Werkzeugs für das Modell (JSON-Schema der Parameter)."""

    name: str
    description: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """Ein vom Modell gewünschter Werkzeug-Aufruf."""

    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str = ""
    #: Nur bei ``assistant``: Werkzeug-Aufrufe dieser Antwort.
    tool_calls: tuple[ToolCall, ...] = ()
    #: Nur bei ``tool``: auf welchen Aufruf sich das Ergebnis bezieht.
    tool_call_id: str | None = None
    #: Nur bei ``tool``: Name des Werkzeugs.
    name: str | None = None
    #: Anbieterspezifische Zusatzdaten (z. B. Gemini-"thought signatures"),
    #: die beim erneuten Senden an denselben Anbieter erhalten bleiben müssen.
    provider_data: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ChatRequest:
    """Eine anbieterneutrale Anfrage an genau ein Modell."""

    model: str
    messages: Sequence[ChatMessage]
    max_tokens: int
    system: str | None = None
    effort: Effort | None = None
    #: Darf der Anbieter bei einer Ablehnung durch Sicherheitsfilter selbst
    #: auf ein anderes Modell ausweichen? (nur Anthropic-API)
    refusal_fallback: bool = False
    tools: Sequence[ToolSpec] = ()
    #: Modellname beim Anbieter (kann vom Katalog-ID abweichen).
    provider_model: str = ""
    #: Modellspezifische Optionen aus ``config.yaml``.
    options: Mapping[str, Any] = field(default_factory=dict)
    #: Zuordnung zu einer Chat-Sitzung (für Werkzeug-Ereignisse in der Oberfläche).
    session_id: str | None = None

    @property
    def target_model(self) -> str:
        return self.provider_model or self.model


@dataclass(frozen=True)
class UsageSegment:
    """Token-Verbrauch eines Modells. Bei Anbieter-Fallbacks gibt es mehrere."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class ChatResponse:
    text: str
    provider: str
    #: Modell, das tatsächlich geantwortet hat (kann vom angefragten abweichen).
    model: str
    requested_model: str
    stop_reason: str | None = None
    usage: list[UsageSegment] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    provider_data: Mapping[str, Any] | None = None
    refused: bool = False
    refusal_category: str | None = None
    provider_fallback_used: bool = False
    #: Kosten sind durch ein Abo gedeckt (z. B. Claude Pro über Claude Code).
    subscription: bool = False
    # Vom Router ergänzt:
    task: TaskKind | None = None
    cost_usd: float = 0.0
    latency_ms: int | None = None
    #: Modelle, die vorher ausgefallen sind und übersprungen wurden.
    router_fallbacks: list[str] = field(default_factory=list)

    @property
    def input_tokens(self) -> int:
        return sum(s.input_tokens + s.cache_read_tokens + s.cache_write_tokens for s in self.usage)

    @property
    def output_tokens(self) -> int:
        return sum(s.output_tokens for s in self.usage)

    @property
    def truncated(self) -> bool:
        return self.stop_reason in {"max_tokens", "length", "MAX_TOKENS"}


@dataclass(frozen=True)
class StreamChunk:
    """Teil einer gestreamten Antwort: Text-Häppchen oder – zum Schluss – die Antwort."""

    text: str = ""
    response: ChatResponse | None = None


class LLMProvider(ABC):
    """Schnittstelle, die jedes Anbieter-Plugin implementiert."""

    #: Eindeutiger Name, so wie er in ``config.yaml`` unter ``router.providers`` steht.
    name: ClassVar[str] = ""
    #: Name des API-Keys, falls ``api_key_name`` in der Konfiguration fehlt.
    #: ``None`` = der Anbieter braucht keinen Key (lokal oder eigener Login).
    default_api_key_name: ClassVar[str | None] = None

    def __init__(
        self, config: ProviderConfig, secrets: SecretStore, *, instance_name: str | None = None
    ) -> None:
        self.config = config
        self._secrets = secrets
        self._api_key: str | None = None
        #: Name des Eintrags in ``config.yaml`` (bei mehreren Instanzen eines Typs verschieden).
        self.instance_name = instance_name or self.name

    @property
    def api_key_name(self) -> str | None:
        return self.config.api_key_name or self.default_api_key_name

    def api_key(self) -> str | None:
        """Liest den API-Key (einmalig) aus dem SecretStore."""
        if self._api_key is None and self.api_key_name:
            self._api_key = self._secrets.get(self.api_key_name)
        return self._api_key

    def refresh(self) -> None:
        """Vergisst zwischengespeicherte Zustände (z. B. nach neuem API-Key)."""
        self._api_key = None

    def unavailable_reason(self) -> str | None:
        """``None``, wenn nutzbar – sonst eine kurze Begründung für Diagnose und UI."""
        if not self.config.enabled:
            return "Anbieter deaktiviert"
        if self.api_key_name and not self.api_key():
            return f"kein API-Key ({self.api_key_name})"
        return None

    def model_unavailable_reason(self, model: ModelConfig) -> str | None:
        """Prüfung pro Modell (z. B. ob ein lokales Modell installiert ist)."""
        return None

    def tools_unavailable_reason(self, model: ModelConfig) -> str | None:
        """``None``, wenn das Modell Werkzeuge nutzen kann."""
        return None if model.supports_tools else "keine Werkzeug-Unterstützung"

    @abstractmethod
    def stream(self, request: ChatRequest) -> AsyncGenerator[StreamChunk, None]:
        """Streamt die Antwort. Das letzte Element enthält ``response``.

        Fehler müssen als :class:`~jarvis.router.errors.ProviderError` (oder
        Unterklasse) geworfen werden, damit der Router richtig reagieren kann.
        """

    async def complete(self, request: ChatRequest) -> ChatResponse:
        """Wie :meth:`stream`, liefert aber nur die fertige Antwort."""
        async for chunk in self.stream(request):
            if chunk.response is not None:
                return chunk.response
        raise RuntimeError(f"Anbieter {self.name} hat keine abschließende Antwort geliefert")

    async def aclose(self) -> None:
        """Gibt Ressourcen (HTTP-Verbindungen) frei."""
        return None
