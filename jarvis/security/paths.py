"""Regeln für Dateizugriffe der Werkzeuge.

* Lesen: erlaubt, außer Dateien mit Secrets (``.env``, Schlüssel, Tokens) und
  JARVIS' eigene Zugangsdaten.
* Neue Dateien schreiben: ohne Rückfrage nur im JARVIS-Arbeitsordner und in
  den Ordnern aus ``security.write_allowed_dirs``; sonst mit Bestätigung.
* Überschreiben, Verschieben, Löschen: immer mit Bestätigung.
* Systemordner und JARVIS' eigener Programmcode: nie (Code ändert JARVIS nur
  über die Selbstverbesserung mit deiner Freigabe).
"""

from __future__ import annotations

import os
from fnmatch import fnmatch
from pathlib import Path

from jarvis.core.config import SecurityConfig
from jarvis.security.permissions import Level


def _expand(raw: str, workspace: Path) -> Path:
    if raw.startswith("@workspace"):
        rest = raw[len("@workspace") :].lstrip("/\\")
        return (workspace / rest).resolve()
    return Path(os.path.expandvars(raw)).expanduser().resolve()


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


class PathPolicy:
    def __init__(
        self,
        config: SecurityConfig,
        *,
        workspace: Path,
        data_dir: Path,
        project_root: Path,
    ) -> None:
        self.workspace = workspace.resolve()
        self._config = config
        self._data_dir = data_dir.resolve()
        self._project_root = project_root.resolve()
        self._write_dirs = [_expand(d, self.workspace) for d in config.write_allowed_dirs]
        self._protected = [Path(d).expanduser().resolve() for d in config.protected_dirs]
        self._secret_dirs = [
            self._data_dir / "google",
            self._data_dir / "api_token",
            Path.home() / ".claude",
            Path.home() / ".ssh",
        ]

    def resolve(self, raw: str) -> Path:
        """Relativ angegebene Pfade beziehen sich auf den Arbeitsordner."""
        text = os.path.expandvars(raw.strip().strip('"'))
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        return path.resolve()

    def is_secret(self, path: Path) -> bool:
        name = path.name.lower()
        if any(fnmatch(name, pattern.lower()) for pattern in self._config.secret_file_patterns):
            return True
        return any(_inside(path, d) or path == d for d in self._secret_dirs)

    def is_protected(self, path: Path) -> bool:
        if any(_inside(path, d) for d in self._protected):
            return True
        if _inside(path, self._project_root):
            return True
        # JARVIS-Daten (Datenbank, Logs, Tokens) – nur der Arbeitsordner ist frei.
        return _inside(path, self._data_dir) and not _inside(path, self.workspace)

    def read_level(self, path: Path) -> Level:
        return Level.FORBIDDEN if self.is_secret(path) else Level.AUTO

    def write_level(self, path: Path, *, overwrite: bool) -> Level:
        if self.is_protected(path) or self.is_secret(path):
            return Level.FORBIDDEN
        if not overwrite and any(_inside(path, d) for d in self._write_dirs):
            return Level.AUTO
        return Level.CONFIRM

    def delete_level(self, path: Path) -> Level:
        if self.is_protected(path) or self.is_secret(path):
            return Level.FORBIDDEN
        return Level.CONFIRM
