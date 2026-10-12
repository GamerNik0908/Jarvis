"""Wiederverwendbare Rich-Darstellungen für Terminal-Chat und CLI-Befehle."""

from __future__ import annotations

from typing import Literal

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from jarvis.core.hardware import HardwareInfo, ModelRecommendation
from jarvis.router.base import ChatResponse
from jarvis.router.costs import BudgetStatus, CostTracker
from jarvis.router.router import ModelRouter


def fmt_usd(value: float) -> str:
    return f"${value:.4f}" if value < 1 else f"${value:.2f}"


def fmt_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def usage_line(response: ChatResponse, budget: BudgetStatus) -> Text:
    """Kompakte Zeile nach jeder Antwort: Modell · Tokens · Kosten · Budget."""
    parts = [
        response.model,
        f"{fmt_tokens(response.input_tokens)} in / {fmt_tokens(response.output_tokens)} out",
        fmt_usd(response.cost_usd),
        f"heute {fmt_usd(budget.spent_today)} / {fmt_usd(budget.daily_limit)}",
    ]
    if response.latency_ms is not None:
        parts.insert(3, f"{response.latency_ms / 1000:.1f} s")
    return Text(" · ".join(parts), style="dim")


def response_notes(response: ChatResponse, budget: BudgetStatus) -> list[Text]:
    """Hinweise zu Fallbacks, Ablehnungen, Abschneiden und Budget."""
    notes: list[Text] = []
    if response.router_fallbacks:
        failed = ", ".join(response.router_fallbacks)
        notes.append(Text(f"↪ Fallback: {failed} nicht erreichbar → {response.model}", "yellow"))
    if response.provider_fallback_used:
        notes.append(
            Text(
                f"↪ Anthropic hat die Anfrage serverseitig an {response.model} weitergeleitet "
                "(Sicherheitsfilter-Fallback).",
                "yellow",
            )
        )
    if response.refused:
        category = f" (Kategorie: {response.refusal_category})" if response.refusal_category else ""
        notes.append(Text(f"⚠ Das Modell hat die Anfrage abgelehnt{category}.", "yellow"))
    if response.truncated:
        notes.append(Text("⚠ Antwort wurde abgeschnitten (max_tokens erreicht).", "yellow"))
    if budget.warning:
        ratio = max(budget.daily_ratio, budget.monthly_ratio)
        notes.append(
            Text(f"⚠ Budget-Warnung: {ratio:.0%} verbraucht – {budget.describe()}", "yellow")
        )
    return notes


def hardware_view(hardware: HardwareInfo, recommendation: ModelRecommendation) -> RenderableType:
    system = Table(title="Hardware", show_header=False, title_justify="left")
    system.add_column(style="bold")
    system.add_column()
    system.add_row("System", f"{hardware.os_version} ({hardware.machine})")
    system.add_row("CPU", hardware.cpu_name)
    system.add_row(
        "Kerne", f"{hardware.cpu_physical_cores} physisch / {hardware.cpu_logical_cores} logisch"
    )
    system.add_row("RAM", f"{hardware.ram_total_gb:g} GB (frei: {hardware.ram_available_gb:g} GB)")
    if hardware.disk_free_gb is not None:
        system.add_row("Speicher frei", f"{hardware.disk_free_gb:g} GB")
    system.add_row("Python", hardware.python_version)

    gpus = Table(title="Grafikkarten", title_justify="left")
    for column in ("Name", "Hersteller", "VRAM", "frei", "Treiber", "nutzbar"):
        gpus.add_column(column)
    for gpu in hardware.gpus:
        gpus.add_row(
            gpu.name,
            gpu.vendor,
            f"{gpu.vram_total_gb:g} GB" if gpu.vram_total_mb else "?",
            f"{gpu.vram_free_mb / 1024:.1f} GB" if gpu.vram_free_mb is not None else "–",
            gpu.driver or "–",
            "✔" if gpu.accelerated else "–",
        )
    if not hardware.gpus:
        gpus.add_row("keine erkannt", "", "", "", "", "")

    rec = Table(
        title=f"Empfohlene lokale Modelle (Stufe '{recommendation.tier}', ab Phase 3)",
        show_header=False,
        title_justify="left",
    )
    rec.add_column(style="bold")
    rec.add_column()
    rec.add_row("Allgemein", recommendation.general)
    rec.add_row("Code", recommendation.coder)
    rec.add_row("Schnell", recommendation.fast)
    rec.add_row("Embeddings", recommendation.embedding)
    rec.add_row(
        "Whisper (STT)",
        f"{recommendation.whisper_model} auf {recommendation.whisper_device} "
        f"({recommendation.whisper_compute_type})",
    )
    parts: list[RenderableType] = [system, gpus, rec]
    parts.extend(Text(f"• {note}", style="yellow") for note in recommendation.notes)
    commands = "; ".join(recommendation.ollama_pull_commands())
    parts.append(Text(f"Vorbereiten für Phase 3 (optional): {commands}", style="dim"))
    return Group(*parts)


def models_view(router: ModelRouter) -> RenderableType:
    models = Table(title="Modelle (Preise in $ pro 1 Mio. Tokens)", title_justify="left")
    models.add_column("ID", no_wrap=True)
    models.add_column("Anbieter")
    models.add_column("Q/T", justify="right")
    models.add_column("ein / aus", justify="right", no_wrap=True)
    models.add_column("Status")
    for model in router.models():
        reason = router.model_unavailable_reason(model)
        models.add_row(
            model.id,
            model.provider,
            f"{model.quality}/{model.speed}",
            f"{model.input_cost_per_mtok:.2f} / {model.output_cost_per_mtok:.2f}",
            Text("bereit", "green") if reason is None else Text(reason, "yellow"),
        )

    routes = Table(title="Routing (Aufgabe → Fallback-Kette)", title_justify="left")
    routes.add_column("Aufgabe", style="bold")
    routes.add_column("Modelle (effort)")
    for task, targets in router.config.routes.items():
        chain = " → ".join(t.model + (f" ({t.effort})" if t.effort else "") for t in targets)
        routes.add_row(task.value, Text(chain))
    return Group(models, routes)


def costs_view(costs: CostTracker) -> RenderableType:
    status = costs.status()
    head = Text(status.describe(), style="red" if status.exceeded else "bold")
    parts: list[RenderableType] = [head]
    periods: tuple[tuple[Literal["day", "month"], str], ...] = (
        ("day", "Heute"),
        ("month", "Dieser Monat"),
    )
    for period, title in periods:
        rows = costs.breakdown(period)
        table = Table(title=title, title_justify="left")
        for column in ("Modell", "Aufrufe", "Tokens ein", "Tokens aus", "Kosten"):
            table.add_column(column)
        for row in rows:
            table.add_row(
                row.model,
                str(row.calls),
                fmt_tokens(row.input_tokens),
                fmt_tokens(row.output_tokens),
                fmt_usd(row.cost_usd),
            )
        if not rows:
            table.add_row("–", "0", "0", "0", fmt_usd(0))
        parts.append(table)
    return Group(*parts)
