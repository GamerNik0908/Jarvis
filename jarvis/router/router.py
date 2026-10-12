"""Der KI-Router: wählt pro Teilschritt das Modell, überwacht Budget und Kosten, macht Fallbacks.

Phase 1: Die Auswahl folgt der Routing-Tabelle aus ``config.yaml`` (Aufgabenart →
geordnete Fallback-Kette). Ab Phase 3 kommt eine automatische Bewertung nach
Qualität, Kosten, Geschwindigkeit und Datenschutz (lokal/Cloud) hinzu – die
Schnittstelle dieser Klasse bleibt dabei gleich.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncGenerator, Mapping, Sequence

from jarvis.core.config import ModelConfig, RouterConfig, RouteTarget
from jarvis.core.types import TaskKind
from jarvis.router.base import ChatMessage, ChatRequest, ChatResponse, LLMProvider, StreamChunk
from jarvis.router.costs import BudgetStatus, CostTracker, UsageRecord, calculate_cost
from jarvis.router.errors import (
    BudgetExceededError,
    NoModelAvailableError,
    ProviderAuthError,
    ProviderError,
    ProviderUnavailableError,
)

log = logging.getLogger(__name__)

#: Grobe Schätzung für abgebrochene Streams, bei denen der Anbieter keine Zahlen liefert.
_CHARS_PER_TOKEN = 4


class ModelRouter:
    """Vermittelt Anfragen an das jeweils passende Modell."""

    def __init__(
        self,
        config: RouterConfig,
        providers: Mapping[str, LLMProvider],
        costs: CostTracker,
    ) -> None:
        self._config = config
        self._providers = dict(providers)
        self._costs = costs
        self._models = {m.id: m for m in config.models}
        #: Anbieter, die in dieser Sitzung wegen Auth-Fehlern gesperrt wurden.
        self._blocked_providers: dict[str, str] = {}

    # ------------------------------------------------------------------ Abfragen

    @property
    def costs(self) -> CostTracker:
        return self._costs

    @property
    def config(self) -> RouterConfig:
        return self._config

    @property
    def providers(self) -> Mapping[str, LLMProvider]:
        return self._providers

    def model(self, model_id: str) -> ModelConfig | None:
        return self._models.get(model_id)

    def models(self) -> list[ModelConfig]:
        return list(self._models.values())

    def route(self, task: TaskKind) -> list[RouteTarget]:
        """Fallback-Kette für eine Aufgabenart (ohne eigene Route: die von ``chat``)."""
        return list(self._config.routes.get(task) or self._config.routes.get(TaskKind.CHAT, []))

    def model_unavailable_reason(self, model: ModelConfig) -> str | None:
        if not model.enabled:
            return "Modell deaktiviert"
        provider = self._providers.get(model.provider)
        if provider is None:
            return f"Anbieter '{model.provider}' nicht geladen"
        blocked = self._blocked_providers.get(model.provider)
        if blocked:
            return blocked
        return provider.unavailable_reason()

    def plan(
        self, task: TaskKind, *, pinned_model: str | None = None
    ) -> tuple[list[RouteTarget], list[str]]:
        """Nutzbare Kandidaten in Reihenfolge und Gründe für übersprungene."""
        if pinned_model is not None:
            if pinned_model not in self._models:
                raise NoModelAvailableError(task, [f"unbekanntes Modell '{pinned_model}'"])
            pinned = next((t for t in self.route(task) if t.model == pinned_model), None)
            targets = [pinned or RouteTarget(model=pinned_model)]
        else:
            targets = self.route(task)
        usable: list[RouteTarget] = []
        skipped: list[str] = []
        for target in targets:
            reason = self.model_unavailable_reason(self._models[target.model])
            if reason:
                skipped.append(f"{target.model}: {reason}")
            else:
                usable.append(target)
        return usable, skipped

    # ------------------------------------------------------------------ Anfragen

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        task: TaskKind = TaskKind.CHAT,
        system: str | None = None,
        pinned_model: str | None = None,
        project_id: str | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        """Streamt die Antwort des ersten funktionierenden Modells der Route.

        Fallback auf das nächste Modell gibt es nur, solange noch kein Text
        ausgegeben wurde – eine halbe Antwort lässt sich nicht nahtlos von einem
        anderen Modell fortsetzen. Ein fest gewähltes Modell (``pinned_model``)
        hat keinen Fallback.
        """
        targets, reasons = self.plan(task, pinned_model=pinned_model)
        budget_block: BudgetStatus | None = None
        failed: list[str] = []

        for target in targets:
            model = self._models[target.model]
            # Erneut prüfen: Ein Auth-Fehler weiter oben in der Kette sperrt den Anbieter.
            blocked = self._blocked_providers.get(model.provider)
            if blocked:
                reasons.append(f"{model.id}: {blocked}")
                continue
            if model.is_paid:
                status = self._costs.status()
                if status.exceeded:
                    budget_block = status
                    reasons.append(f"{model.id}: Budget erreicht")
                    continue

            provider = self._providers[model.provider]
            request = ChatRequest(
                model=model.id,
                messages=list(messages),
                system=system,
                max_tokens=min(
                    target.max_tokens or self._config.default_max_tokens, model.max_output_tokens
                ),
                effort=target.effort if model.supports_effort else None,
                refusal_fallback=model.refusal_fallback,
            )
            started = time.monotonic()
            emitted_chars = 0
            final: ChatResponse | None = None
            try:
                async with contextlib.aclosing(provider.stream(request)) as chunks:
                    async for chunk in chunks:
                        if chunk.response is not None:
                            final = chunk.response
                            break
                        if chunk.text:
                            emitted_chars += len(chunk.text)
                            yield chunk
                if final is None:
                    raise ProviderUnavailableError(
                        "Stream endete ohne Abschluss", provider=provider.name, model=model.id
                    )
            except ProviderError as exc:
                self._record_failure(model, task, started, project_id, exc)
                if isinstance(exc, ProviderAuthError):
                    self._blocked_providers[provider.name] = f"gesperrt nach Fehler: {exc}"
                if emitted_chars or not exc.fallback_allowed or pinned_model is not None:
                    raise
                log.warning("Modell %s fehlgeschlagen (%s) – versuche nächstes", model.id, exc)
                reasons.append(f"{model.id}: {exc}")
                failed.append(model.id)
                continue
            except (asyncio.CancelledError, GeneratorExit):
                self._record_cancelled(model, task, started, project_id, request, emitted_chars)
                raise

            response = self._finalize(final, model, task, started, project_id, failed)
            yield StreamChunk(response=response)
            return

        if budget_block is not None and not failed:
            raise BudgetExceededError(budget_block)
        raise NoModelAvailableError(task, reasons)

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        task: TaskKind = TaskKind.CHAT,
        system: str | None = None,
        pinned_model: str | None = None,
        project_id: str | None = None,
    ) -> ChatResponse:
        """Wie :meth:`stream`, liefert aber nur die fertige Antwort."""
        stream = self.stream(
            messages, task=task, system=system, pinned_model=pinned_model, project_id=project_id
        )
        async with contextlib.aclosing(stream) as chunks:
            async for chunk in chunks:
                if chunk.response is not None:
                    return chunk.response
        raise RuntimeError("Router hat keine Antwort geliefert")  # pragma: no cover

    async def aclose(self) -> None:
        for provider in self._providers.values():
            try:
                await provider.aclose()
            except Exception:
                log.exception("Fehler beim Schließen von Anbieter %s", provider.name)

    # ------------------------------------------------------------------ Kosten

    def _finalize(
        self,
        response: ChatResponse,
        requested: ModelConfig,
        task: TaskKind,
        started: float,
        project_id: str | None,
        failed: list[str],
    ) -> ChatResponse:
        latency_ms = int((time.monotonic() - started) * 1000)
        segments = response.usage
        total = 0.0
        if not segments:
            self._costs.record(
                UsageRecord(
                    provider=response.provider,
                    model=response.model,
                    task=task.value,
                    latency_ms=latency_ms,
                    error="refusal" if response.refused else None,
                    project_id=project_id,
                )
            )
        for index, segment in enumerate(segments):
            priced_as = self._models.get(segment.model)
            if priced_as is None:
                log.warning(
                    "Kein Preis für Modell %s hinterlegt – berechne mit %s",
                    segment.model,
                    requested.id,
                )
                priced_as = requested
            cost = calculate_cost(
                priced_as,
                input_tokens=segment.input_tokens,
                output_tokens=segment.output_tokens,
                cache_read_tokens=segment.cache_read_tokens,
                cache_write_tokens=segment.cache_write_tokens,
            )
            total += cost
            is_last = index == len(segments) - 1
            self._costs.record(
                UsageRecord(
                    provider=response.provider,
                    model=segment.model,
                    task=task.value,
                    input_tokens=segment.input_tokens,
                    output_tokens=segment.output_tokens,
                    cache_read_tokens=segment.cache_read_tokens,
                    cache_write_tokens=segment.cache_write_tokens,
                    cost_usd=cost,
                    latency_ms=latency_ms if is_last else None,
                    error="refusal" if response.refused and is_last else None,
                    project_id=project_id,
                )
            )
        response.task = task
        response.cost_usd = total
        response.latency_ms = latency_ms
        response.router_fallbacks = list(failed)
        log.info(
            "%s · %s · %d in / %d out · $%.4f · %d ms",
            task.value,
            response.model,
            response.input_tokens,
            response.output_tokens,
            total,
            latency_ms,
        )
        return response

    def _record_failure(
        self,
        model: ModelConfig,
        task: TaskKind,
        started: float,
        project_id: str | None,
        exc: ProviderError,
    ) -> None:
        self._costs.record(
            UsageRecord(
                provider=model.provider,
                model=model.id,
                task=task.value,
                latency_ms=int((time.monotonic() - started) * 1000),
                success=False,
                error=f"{type(exc).__name__}: {exc}"[:500],
                project_id=project_id,
            )
        )

    def _record_cancelled(
        self,
        model: ModelConfig,
        task: TaskKind,
        started: float,
        project_id: str | None,
        request: ChatRequest,
        emitted_chars: int,
    ) -> None:
        """Abgebrochene Streams werden trotzdem berechnet – wir schätzen konservativ.

        Der Anbieter berechnet alles, was bis zum Abbruch erzeugt wurde, liefert
        dafür aber keine Zahlen mehr. Damit das Budget nicht unterlaufen wird,
        schätzen wir Eingabe und bisherige Ausgabe anhand der Zeichenzahl.
        """
        prompt_chars = sum(len(m.content) for m in request.messages) + len(request.system or "")
        input_tokens = prompt_chars // _CHARS_PER_TOKEN
        output_tokens = emitted_chars // _CHARS_PER_TOKEN
        self._costs.record(
            UsageRecord(
                provider=model.provider,
                model=model.id,
                task=task.value,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=calculate_cost(
                    model, input_tokens=input_tokens, output_tokens=output_tokens
                ),
                latency_ms=int((time.monotonic() - started) * 1000),
                success=False,
                estimated=True,
                error="abgebrochen",
                project_id=project_id,
            )
        )
