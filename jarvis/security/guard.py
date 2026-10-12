"""Schutzregeln für die Selbstverbesserung.

Eine Änderung wird verworfen, wenn sie geschützte Dateien berührt oder in
``config.yaml`` die Abschnitte für Sicherheit, Budget, Secrets oder die
Selbstverbesserung selbst verändert. Diese Datei liegt in ``jarvis/security/``
und ist damit selbst geschützt.
"""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatch
from typing import Any

import yaml

from jarvis.core.config import PROTECTED_CONFIG_SECTIONS

#: Pfade relativ zum Projektordner (``/`` als Trenner). Ordner enden mit ``/``.
PROTECTED_PATHS: tuple[str, ...] = (
    "jarvis/security/",
    "jarvis/self_improve/",
    "jarvis/core/supervisor.py",
    ".env",
    ".env.*",
    "config.local.yaml",
    ".git/",
    ".gitattributes",
)


def normalize(path: str) -> str:
    text = path.replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text


def is_protected(path: str) -> bool:
    rel = normalize(path)
    for rule in PROTECTED_PATHS:
        if rule.endswith("/"):
            if rel.startswith(rule) or rel == rule.rstrip("/"):
                return True
        elif fnmatch(rel, rule):
            return True
    return False


def protected_changes(changed_files: Iterable[str]) -> list[str]:
    return sorted({normalize(p) for p in changed_files if is_protected(p)})


def config_section_changes(before: str | None, after: str | None) -> list[str]:
    """Welche geschützten Abschnitte von ``config.yaml`` unterscheiden sich?"""
    old = _load(before)
    new = _load(after)
    return [s for s in PROTECTED_CONFIG_SECTIONS if old.get(s) != new.get(s)]


def _load(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return {"__invalid__": True}
    return data if isinstance(data, dict) else {}


def violations(
    changed_files: Iterable[str],
    *,
    config_before: str | None = None,
    config_after: str | None = None,
) -> list[str]:
    """Liste verständlicher Verstöße (leer = Änderung ist zulässig)."""
    changed = list(changed_files)  # wird zweimal durchlaufen – auch Generatoren erlauben
    problems = [f"geschützte Datei geändert: {p}" for p in protected_changes(changed)]
    files = {normalize(p) for p in changed}
    if "config.yaml" in files:
        for section in config_section_changes(config_before, config_after):
            problems.append(f"geschützter Konfigurationsabschnitt geändert: {section}")
    return problems
