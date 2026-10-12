"""Kostenberechnung, Verbrauchsprotokoll und Budget-Limits.

Jeder Modellaufruf wird mit Tokens und Kosten in SQLite protokolliert. Daraus
ergeben sich Tages-/Monatssummen (lokale Zeit) und die harten Budget-Grenzen.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from jarvis.core.config import BudgetConfig, ModelConfig
from jarvis.core.db import Database
from jarvis.router.errors import BudgetExceededError

_DEFAULT_CACHE_READ_FACTOR = 0.1
_DEFAULT_CACHE_WRITE_FACTOR = 1.25


def calculate_cost(
    model: ModelConfig,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Kosten eines Aufrufs in USD anhand der Preise im Modellkatalog."""
    in_price = model.input_cost_per_mtok
    out_price = model.output_cost_per_mtok
    prompt_tokens = input_tokens + cache_read_tokens + cache_write_tokens
    threshold = model.long_context_threshold_tokens
    if threshold is not None and prompt_tokens > threshold:
        in_price = model.long_input_cost_per_mtok or in_price
        out_price = model.long_output_cost_per_mtok or out_price
    read_price = (
        model.cache_read_cost_per_mtok
        if model.cache_read_cost_per_mtok is not None
        else in_price * _DEFAULT_CACHE_READ_FACTOR
    )
    write_price = (
        model.cache_write_cost_per_mtok
        if model.cache_write_cost_per_mtok is not None
        else in_price * _DEFAULT_CACHE_WRITE_FACTOR
    )
    total = (
        input_tokens * in_price
        + output_tokens * out_price
        + cache_read_tokens * read_price
        + cache_write_tokens * write_price
    )
    return total / 1_000_000


@dataclass(frozen=True)
class UsageRecord:
    """Ein protokollierter Modellaufruf (oder ein Teil davon bei Fallbacks)."""

    provider: str
    model: str
    task: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int | None = None
    success: bool = True
    estimated: bool = False
    error: str | None = None
    project_id: str | None = None


@dataclass(frozen=True)
class BudgetStatus:
    spent_today: float
    spent_month: float
    daily_limit: float
    monthly_limit: float
    warn_at_ratio: float

    @property
    def daily_exceeded(self) -> bool:
        return self.spent_today >= self.daily_limit

    @property
    def monthly_exceeded(self) -> bool:
        return self.spent_month >= self.monthly_limit

    @property
    def exceeded(self) -> bool:
        return self.daily_exceeded or self.monthly_exceeded

    @property
    def daily_ratio(self) -> float:
        return self.spent_today / self.daily_limit if self.daily_limit > 0 else 1.0

    @property
    def monthly_ratio(self) -> float:
        return self.spent_month / self.monthly_limit if self.monthly_limit > 0 else 1.0

    @property
    def paid_disabled(self) -> bool:
        """Budget 0 = kostenpflichtige Modelle sind bewusst abgeschaltet."""
        return self.daily_limit <= 0 or self.monthly_limit <= 0

    @property
    def warning(self) -> bool:
        """Warnschwelle erreicht, aber noch nicht überschritten."""
        if self.exceeded or self.paid_disabled:
            return False
        return max(self.daily_ratio, self.monthly_ratio) >= self.warn_at_ratio

    def describe(self) -> str:
        if self.paid_disabled:
            return (
                "Kostenlos-Modus: kostenpflichtige Modelle sind aus (Budget 0 $). "
                f"Bisher ausgegeben: heute ${self.spent_today:.2f}, Monat ${self.spent_month:.2f}"
            )
        if self.daily_exceeded:
            return (
                f"Tagesbudget erreicht: ${self.spent_today:.2f} von ${self.daily_limit:.2f}. "
                "Kostenpflichtige Modelle sind bis morgen gesperrt (budget.daily_limit_usd)."
            )
        if self.monthly_exceeded:
            return (
                f"Monatsbudget erreicht: ${self.spent_month:.2f} von ${self.monthly_limit:.2f} "
                "(budget.monthly_limit_usd)."
            )
        return (
            f"Heute ${self.spent_today:.2f} / ${self.daily_limit:.2f} · "
            f"Monat ${self.spent_month:.2f} / ${self.monthly_limit:.2f}"
        )


@dataclass(frozen=True)
class ModelUsageSummary:
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


def _local_now() -> datetime:
    return datetime.now().astimezone()


class CostTracker:
    """Schreibt Verbrauchsdaten und prüft Budgets."""

    def __init__(
        self,
        db: Database,
        budget: BudgetConfig,
        *,
        clock: Callable[[], datetime] = _local_now,
    ) -> None:
        self._db = db
        self._budget = budget
        self._clock = clock

    @property
    def budget(self) -> BudgetConfig:
        return self._budget

    def record(self, record: UsageRecord) -> None:
        now = self._clock()
        self._db.execute(
            """
            INSERT INTO model_usage (
                ts, day, month, provider, model, task, input_tokens, output_tokens,
                cache_read_tokens, cache_write_tokens, cost_usd, latency_ms, success,
                estimated, error, project_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now.isoformat(timespec="seconds"),
                now.strftime("%Y-%m-%d"),
                now.strftime("%Y-%m"),
                record.provider,
                record.model,
                record.task,
                record.input_tokens,
                record.output_tokens,
                record.cache_read_tokens,
                record.cache_write_tokens,
                record.cost_usd,
                record.latency_ms,
                int(record.success),
                int(record.estimated),
                record.error,
                record.project_id,
            ),
        )

    def spent_today(self) -> float:
        return self._sum("day", self._clock().strftime("%Y-%m-%d"))

    def spent_this_month(self) -> float:
        return self._sum("month", self._clock().strftime("%Y-%m"))

    def spent_for_project(self, project_id: str) -> float:
        row = self._db.query_one(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM model_usage WHERE project_id = ?",
            (project_id,),
        )
        return float(row[0]) if row else 0.0

    def status(self) -> BudgetStatus:
        return BudgetStatus(
            spent_today=self.spent_today(),
            spent_month=self.spent_this_month(),
            daily_limit=self._budget.daily_limit_usd,
            monthly_limit=self._budget.monthly_limit_usd,
            warn_at_ratio=self._budget.warn_at_ratio,
        )

    def ensure_within_budget(self) -> BudgetStatus:
        """Wirft :class:`BudgetExceededError`, wenn ein Limit erreicht ist."""
        status = self.status()
        if status.exceeded:
            raise BudgetExceededError(status)
        return status

    def breakdown(self, period: Literal["day", "month"] = "day") -> list[ModelUsageSummary]:
        key = self._clock().strftime("%Y-%m-%d" if period == "day" else "%Y-%m")
        column = "day" if period == "day" else "month"
        rows = self._db.query(
            f"""
            SELECT model, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens, SUM(cost_usd) AS cost_usd
            FROM model_usage WHERE {column} = ?
            GROUP BY model ORDER BY cost_usd DESC
            """,
            (key,),
        )
        return [
            ModelUsageSummary(
                model=row["model"],
                calls=int(row["calls"]),
                input_tokens=int(row["input_tokens"] or 0),
                output_tokens=int(row["output_tokens"] or 0),
                cost_usd=float(row["cost_usd"] or 0.0),
            )
            for row in rows
        ]

    def _sum(self, column: Literal["day", "month"], key: str) -> float:
        row = self._db.query_one(
            f"SELECT COALESCE(SUM(cost_usd), 0) FROM model_usage WHERE {column} = ?", (key,)
        )
        return float(row[0]) if row else 0.0
