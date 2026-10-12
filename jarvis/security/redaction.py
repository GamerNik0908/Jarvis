"""Schwärzt Secrets in Texten und Log-Ausgaben.

Zwei Mechanismen ergänzen sich:

1. Jeder Key, den der :class:`~jarvis.security.secrets.SecretStore` ausliest,
   wird hier registriert und danach überall durch ``***REDACTED***`` ersetzt.
2. Typische Key-Formate (``sk-ant-…``, ``sk-…``, ``AIza…``, Bearer-Tokens)
   werden zusätzlich per Muster erkannt – auch wenn sie nie registriert wurden.
"""

from __future__ import annotations

import logging
import re
import threading

REDACTED = "***REDACTED***"
_MIN_SECRET_LENGTH = 8

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), REDACTED),
    (re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}"), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), REDACTED),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-~+/]{16,}=*"), rf"\g<1>{REDACTED}"),
    (
        re.compile(
            r"(?i)\b(x-api-key|api[_-]?key|access[_-]?token|auth[_-]?token|secret|password)"
            r"([\"']?\s*[:=]\s*[\"']?)([^\s\"',;]{6,})"
        ),
        rf"\g<1>\g<2>{REDACTED}",
    ),
)

_lock = threading.Lock()
_known_secrets: set[str] = set()
_sorted_secrets: tuple[str, ...] = ()


def register_secret(value: str | None) -> None:
    """Merkt sich einen Secret-Wert, damit er nie im Klartext geloggt wird."""
    global _sorted_secrets
    if not value:
        return
    value = value.strip()
    if len(value) < _MIN_SECRET_LENGTH:
        return
    with _lock:
        if value in _known_secrets:
            return
        _known_secrets.add(value)
        # Längste zuerst, damit überlappende Werte vollständig ersetzt werden.
        _sorted_secrets = tuple(sorted(_known_secrets, key=len, reverse=True))


def clear_registered_secrets() -> None:
    """Nur für Tests: vergisst alle registrierten Secrets."""
    global _sorted_secrets
    with _lock:
        _known_secrets.clear()
        _sorted_secrets = ()


def redact(text: str) -> str:
    """Ersetzt alle bekannten Secrets und Key-Muster in ``text``."""
    if not text:
        return text
    for secret in _sorted_secrets:
        if secret in text:
            text = text.replace(secret, REDACTED)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Log-Formatter, der die fertige Zeile (inkl. Traceback) schwärzt."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))
