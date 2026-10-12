"""Zugriff auf API-Keys.

Quellen in dieser Reihenfolge:

1. echte Umgebungsvariablen (auch aus ``.env`` geladen),
2. Windows Credential Manager über ``keyring``.

Keys stehen nie im Code oder in ``config.yaml``. Jeder gelesene Wert wird
sofort bei der Log-Schwärzung registriert.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal

import keyring
from dotenv import load_dotenv
from keyring.errors import PasswordDeleteError

from jarvis.security.redaction import register_secret

log = logging.getLogger(__name__)

SecretSource = Literal["env", "keyring"]


class SecretStoreError(Exception):
    """Ein Secret konnte nicht gespeichert oder gelöscht werden."""


class SecretStore:
    """Liest und speichert API-Keys, ohne sie je zu loggen."""

    def __init__(
        self,
        *,
        env_file: Path | None = None,
        use_keyring: bool = True,
        keyring_service: str = "JARVIS",
    ) -> None:
        self._use_keyring = use_keyring
        self._service = keyring_service
        self.env_file = env_file
        if env_file is not None and env_file.is_file():
            # override=False: echte Umgebungsvariablen haben Vorrang vor .env
            # utf-8-sig: verträgt das BOM, das ältere Editoren voranstellen
            load_dotenv(env_file, override=False, encoding="utf-8-sig")
            log.debug(".env geladen: %s", env_file)

    @property
    def keyring_enabled(self) -> bool:
        return self._use_keyring

    def get(self, name: str) -> str | None:
        """Liefert den Wert oder ``None``, wenn der Key nirgends hinterlegt ist."""
        value, _ = self.lookup(name)
        return value

    def lookup(self, name: str) -> tuple[str | None, SecretSource | None]:
        """Wie :meth:`get`, verrät aber zusätzlich die Quelle."""
        env_value = os.environ.get(name, "").strip()
        if env_value:
            register_secret(env_value)
            return env_value, "env"
        if self._use_keyring:
            stored = self._keyring_get(name)
            if stored:
                register_secret(stored)
                return stored, "keyring"
        return None, None

    def set(self, name: str, value: str) -> None:
        """Speichert einen Key im Windows Credential Manager."""
        if not self._use_keyring:
            raise SecretStoreError(
                "keyring ist in der Konfiguration deaktiviert (secrets.use_keyring)"
            )
        value = value.strip()
        if not value:
            raise SecretStoreError("Leerer Wert – nichts gespeichert")
        register_secret(value)
        try:
            keyring.set_password(self._service, name, value)
        except Exception as exc:  # Backends werfen sehr unterschiedliche Fehlertypen
            raise SecretStoreError(
                f"Speichern im Credential Manager fehlgeschlagen: {exc}"
            ) from exc
        log.info("Secret %s im Credential Manager gespeichert", name)

    def delete(self, name: str) -> bool:
        """Löscht einen Key aus dem Credential Manager. ``False``, wenn nicht vorhanden."""
        if not self._use_keyring:
            raise SecretStoreError(
                "keyring ist in der Konfiguration deaktiviert (secrets.use_keyring)"
            )
        try:
            keyring.delete_password(self._service, name)
        except PasswordDeleteError:
            return False
        except Exception as exc:
            raise SecretStoreError(f"Löschen fehlgeschlagen: {exc}") from exc
        log.info("Secret %s aus dem Credential Manager gelöscht", name)
        return True

    def _keyring_get(self, name: str) -> str | None:
        try:
            value: str | None = keyring.get_password(self._service, name)
        except Exception as exc:  # z. B. kein Backend verfügbar (Linux ohne Schlüsselbund)
            log.debug("keyring nicht verfügbar: %s", type(exc).__name__)
            return None
        return value.strip() if value else None
