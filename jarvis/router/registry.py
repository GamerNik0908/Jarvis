"""Registrierung von Anbieter-Plugins.

Drei Wege, einen Anbieter bekannt zu machen:

1. eingebaut in ``jarvis/router/providers/`` (mit ``@register_provider``),
2. eigenes Modul in ``router.provider_modules`` der ``config.yaml``,
3. installiertes Paket mit Entry-Point in der Gruppe ``jarvis.providers``.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterable
from importlib.metadata import entry_points
from typing import TypeVar

from jarvis.core.config import RouterConfig
from jarvis.router.base import LLMProvider
from jarvis.security.secrets import SecretStore

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "jarvis.providers"

P = TypeVar("P", bound=type[LLMProvider])

_REGISTRY: dict[str, type[LLMProvider]] = {}


def register_provider(cls: P) -> P:
    """Klassen-Dekorator: macht einen Anbieter unter ``cls.name`` verfügbar."""
    if not cls.name:
        raise ValueError(f"{cls.__qualname__} braucht ein Klassenattribut 'name'")
    existing = _REGISTRY.get(cls.name)
    if existing is not None and existing is not cls:
        log.warning("Anbieter '%s' wird durch %s ersetzt", cls.name, cls.__qualname__)
    _REGISTRY[cls.name] = cls
    return cls


def registered_providers() -> dict[str, type[LLMProvider]]:
    return dict(_REGISTRY)


def load_provider_plugins(extra_modules: Iterable[str] = ()) -> None:
    """Importiert eingebaute, konfigurierte und per Entry-Point installierte Anbieter."""
    importlib.import_module("jarvis.router.providers")
    for module_name in extra_modules:
        try:
            importlib.import_module(module_name)
        except Exception:
            log.exception("Anbieter-Modul '%s' konnte nicht geladen werden", module_name)
    for entry_point in entry_points(group=ENTRY_POINT_GROUP):
        try:
            loaded = entry_point.load()
        except Exception:
            log.exception("Entry-Point '%s' konnte nicht geladen werden", entry_point.name)
            continue
        if isinstance(loaded, type) and issubclass(loaded, LLMProvider):
            register_provider(loaded)


def create_providers(config: RouterConfig, secrets: SecretStore) -> dict[str, LLMProvider]:
    """Erzeugt für jeden konfigurierten Anbieter eine Instanz."""
    load_provider_plugins(config.provider_modules)
    providers: dict[str, LLMProvider] = {}
    for name, provider_config in config.providers.items():
        kind = provider_config.type or name
        cls = _REGISTRY.get(kind)
        if cls is None:
            log.error(
                "Unbekannter Anbieter-Typ '%s' in config.yaml – kein Plugin registriert", kind
            )
            continue
        providers[name] = cls(provider_config, secrets, instance_name=name)
    return providers
