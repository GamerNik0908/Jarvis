"""Fehlerklassen des KI-Routers.

``fallback_allowed`` entscheidet, ob der Router bei diesem Fehler das nächste
Modell der Route versuchen darf. Bei ungültigen Anfragen (400) wäre ein
Fallback sinnlos – derselbe Fehler würde sich nur wiederholen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from jarvis.router.costs import BudgetStatus


class RouterError(Exception):
    """Basisklasse aller Router-Fehler."""


class ProviderError(RouterError):
    """Ein Anbieter konnte die Anfrage nicht erfüllen."""

    fallback_allowed: ClassVar[bool] = True

    def __init__(self, message: str, *, provider: str, model: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model


class ProviderAuthError(ProviderError):
    """API-Key fehlt/ungültig, keine Berechtigung oder Abrechnungsproblem."""


class ProviderUnavailableError(ProviderError):
    """Vorübergehend nicht erreichbar: Rate-Limit, Überlastung, Netzwerk, Timeout.

    ``retry_after_s`` > 0 bedeutet: Modell für diese Zeit pausieren
    (z. B. Free-Tier-Limit von Gemini oder Nutzungslimit des Claude-Abos).
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        model: str | None = None,
        retry_after_s: float = 0.0,
    ) -> None:
        super().__init__(message, provider=provider, model=model)
        self.retry_after_s = retry_after_s


class ModelNotFoundError(ProviderError):
    """Modell existiert nicht (mehr) oder ist für dieses Konto nicht freigeschaltet."""


class ProviderRequestError(ProviderError):
    """Die Anfrage selbst ist ungültig – ein anderes Modell hilft nicht."""

    fallback_allowed: ClassVar[bool] = False


class BudgetExceededError(RouterError):
    """Das Tages- oder Monatsbudget für kostenpflichtige Modelle ist ausgeschöpft."""

    def __init__(self, status: BudgetStatus) -> None:
        super().__init__(status.describe())
        self.status = status


class NoModelAvailableError(RouterError):
    """Für die Aufgabe ist kein Modell nutzbar."""

    def __init__(self, task: str, reasons: list[str]) -> None:
        detail = "; ".join(reasons) if reasons else "keine Modelle konfiguriert"
        super().__init__(f"Kein Modell für Aufgabe '{task}' verfügbar ({detail})")
        self.task = task
        self.reasons = reasons
