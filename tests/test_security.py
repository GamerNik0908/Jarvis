from __future__ import annotations

import logging
from pathlib import Path

import pytest

from jarvis.security.redaction import REDACTED, RedactingFormatter, redact, register_secret
from jarvis.security.secrets import SecretStore, SecretStoreError


def test_registered_secret_is_redacted() -> None:
    register_secret("mein-geheimer-wert-123")
    assert redact("key=mein-geheimer-wert-123!") == f"key={REDACTED}!"


def test_short_values_are_not_registered() -> None:
    register_secret("abc")
    assert redact("abc") == "abc"


@pytest.mark.parametrize(
    "text",
    [
        "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        "AIzaSyA-1234567890abcdefghijklmnopqrstu",
    ],
)
def test_key_patterns_are_redacted(text: str) -> None:
    assert redact(f"Fehler mit {text} im Header") == f"Fehler mit {REDACTED} im Header"


def test_assignment_and_bearer_patterns_keep_the_label() -> None:
    assert redact("api_key=abcdef123456") == f"api_key={REDACTED}"
    assert redact("Authorization: Bearer abcdefghijklmnopqrstuvwxyz") == (
        f"Authorization: Bearer {REDACTED}"
    )


def test_formatter_redacts_messages_and_tracebacks() -> None:
    register_secret("super-secret-token-xyz")
    formatter = RedactingFormatter("%(message)s")
    try:
        raise RuntimeError("token super-secret-token-xyz abgelehnt")
    except RuntimeError:
        record = logging.LogRecord(
            "t", logging.ERROR, __file__, 1, "Key %s", ("super-secret-token-xyz",), None
        )
        import sys

        record.exc_info = sys.exc_info()
    output = formatter.format(record)
    assert "super-secret-token-xyz" not in output
    assert output.count(REDACTED) >= 2


def test_secret_store_prefers_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_TEST_KEY", "  wert-aus-der-umgebung  ")
    store = SecretStore(use_keyring=False)
    assert store.lookup("JARVIS_TEST_KEY") == ("wert-aus-der-umgebung", "env")
    # Der gelesene Wert taucht nie mehr im Klartext in Logs auf.
    assert redact("wert-aus-der-umgebung") == REDACTED


def test_secret_store_treats_empty_values_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_TEST_KEY", "")
    assert SecretStore(use_keyring=False).get("JARVIS_TEST_KEY") is None


def test_secret_store_loads_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("JARVIS_DOTENV_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("JARVIS_DOTENV_KEY=aus-der-datei-123\n", encoding="utf-8")
    store = SecretStore(env_file=env_file, use_keyring=False)
    assert store.get("JARVIS_DOTENV_KEY") == "aus-der-datei-123"
    monkeypatch.delenv("JARVIS_DOTENV_KEY", raising=False)


def test_keyring_operations_fail_cleanly_when_disabled() -> None:
    store = SecretStore(use_keyring=False)
    with pytest.raises(SecretStoreError):
        store.set("X_KEY", "value-12345")
    with pytest.raises(SecretStoreError):
        store.delete("X_KEY")


def test_env_file_with_bom_is_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("JARVIS_BOM_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_bytes("JARVIS_BOM_KEY=mit-bom-gespeichert\n".encode("utf-8-sig"))
    assert SecretStore(env_file=env_file, use_keyring=False).get("JARVIS_BOM_KEY") == (
        "mit-bom-gespeichert"
    )
    monkeypatch.delenv("JARVIS_BOM_KEY", raising=False)
