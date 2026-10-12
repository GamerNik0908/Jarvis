"""Gemeinsame Datentypen und die Schnittstelle für KI-Anbieter.

Ein Anbieter (Anthropic, später OpenAI, Gemini, Ollama, …) ist ein Plugin, das
:class:`LLMProvider` implementiert. Der Router spricht ausschließlich mit
dieser Schnittstelle und kennt keine anbieterspezifischen Details.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Literal

from jarvis.core.config import ProviderConfig
from jarvis.core.types import Effort, TaskKind
from jarvis.security.secrets import SecretStore


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class ChatRequest:
    """Eine anbieterneutrale Anfrage an genau ein Modell."""

    model: str
    messages: Sequence[ChatMessage]
    max_tokens: int
    system: str | None = None
    effort: Effort | None = None
    #: Darf der Anbieter bei einer Ablehnung durch Sicherheitsfilter selbst
    #: auf ein anderes Modell ausweichen? (Anthropic: ``fallbacks="default"``)
    refusal_fallback: bool = False


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
    refused: bool = False
    refusal_category: str | None = None
    provider_fallback_used: bool = False
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
        return self.stop_reason == "max_tokens"


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
    default_api_key_name: ClassVar[str | None] = None

    def __init__(self, config: ProviderConfig, secrets: SecretStore) -> None:
        self.config = config
        self._secrets = secrets
        self._api_key: str | None = None

    @property
    def api_key_name(self) -> str | None:
        return self.config.api_key_name or self.default_api_key_name

    def api_key(self) -> str | None:
        """Liest den API-Key (einmalig) aus dem SecretStore."""
        if self._api_key is None and self.api_key_name:
            self._api_key = self._secrets.get(self.api_key_name)
        return self._api_key

    def unavailable_reason(self) -> str | None:
        """``None``, wenn nutzbar – sonst eine kurze Begründung für Diagnose und UI."""
        if not self.config.enabled:
            return "Anbieter deaktiviert"
        if self.api_key_name and not self.api_key():
            return f"kein API-Key ({self.api_key_name})"
        return None

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
