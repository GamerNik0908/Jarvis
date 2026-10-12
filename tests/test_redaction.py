"""Schwärzen von Zuweisungen wie ``ANTHROPIC_API_KEY=…`` – auch ohne registrierten Wert."""

from __future__ import annotations

import pytest

from jarvis.security.redaction import REDACTED, redact


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Präfixe mit Unterstrich: hier greift ``\b`` nicht.
        ("ANTHROPIC_API_KEY=abcdef123456", f"ANTHROPIC_API_KEY={REDACTED}"),
        ("OPENAI_API_KEY = 'abcdef123456'", f"OPENAI_API_KEY = '{REDACTED}'"),
        ("DB_PASSWORD=hunter2hunter2", f"DB_PASSWORD={REDACTED}"),
        ("client_secret: xyz123456", f"client_secret: {REDACTED}"),
        ("GITHUB_ACCESS_TOKEN=ghp_abcdefghijkl", f"GITHUB_ACCESS_TOKEN={REDACTED}"),
        ("SLACK_AUTH_TOKEN=xoxb-1234567890", f"SLACK_AUTH_TOKEN={REDACTED}"),
        ("x-goog-api-key: abcdefgh1234", f"x-goog-api-key: {REDACTED}"),
        ("dbpassword=hunter2hunter2", f"dbpassword={REDACTED}"),
        # Schon vorher erkannte Formen bleiben erkannt.
        ("api_key=abcdef123456", f"api_key={REDACTED}"),
        ("x-api-key: abcdef123456", f"x-api-key: {REDACTED}"),
        ('{"password": "hunter2hunter2"}', f'{{"password": "{REDACTED}"}}'),
        ("--secret=geheim1234 weiter", f"--secret={REDACTED} weiter"),
        ("Password: abcdefgh", f"Password: {REDACTED}"),
    ],
)
def test_assignments_with_prefix_are_redacted(text: str, expected: str) -> None:
    assert redact(text) == expected


def test_only_the_value_is_replaced_in_longer_text() -> None:
    text = "Start mit ANTHROPIC_API_KEY=abcdef123456 und DB_PASSWORD=hunter2hunter2 fertig"
    assert redact(text) == (
        f"Start mit ANTHROPIC_API_KEY={REDACTED} und DB_PASSWORD={REDACTED} fertig"
    )


@pytest.mark.parametrize(
    "text",
    [
        "password: kurz",  # zu kurz für ein Secret
        "passwords=abcdefgh123",  # anderes Wort
        "default_api_key_name=ANTHROPIC_API_KEY",  # Name der Variable, kein Wert
        "Secretary=Frau Meier-Schulze",
        "Das Passwort ist geheim.",
        "Der API-Key fehlt – bitte in .env eintragen.",
    ],
)
def test_harmless_text_stays_untouched(text: str) -> None:
    assert redact(text) == text
