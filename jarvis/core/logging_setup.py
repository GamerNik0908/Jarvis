"""Logging: rotierende Logdatei + kompakte Konsolenausgabe, beides mit Secret-Schwärzung."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

from jarvis.core.config import LoggingConfig
from jarvis.security.redaction import RedactingFormatter

_FILE_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
_NOISY_LOGGERS = ("httpx", "httpx2", "httpcore", "anthropic", "urllib3", "asyncio", "markdown_it")

_installed: list[logging.Handler] = []


def setup_logging(
    config: LoggingConfig,
    log_dir: Path,
    *,
    role: str = "main",
    console_level: str | None = None,
    console: Console | None = None,
) -> Path:
    """Richtet das Logging ein und gibt den Pfad der Logdatei zurück.

    ``role`` landet im Dateinamen (``jarvis-chat.log``, ``jarvis-worker.log`` …),
    damit mehrere JARVIS-Prozesse sich unter Windows nicht gegenseitig die
    Logdatei sperren. Mehrfacher Aufruf ersetzt die vorherigen Handler.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"jarvis-{role}.log"

    root = logging.getLogger()
    for handler in _installed:
        root.removeHandler(handler)
        handler.close()
    _installed.clear()

    file_level = logging.getLevelNamesMapping()[config.file_level]
    term_level = logging.getLevelNamesMapping()[(console_level or config.console_level).upper()]

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=config.max_bytes,
        backupCount=config.backup_count,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(RedactingFormatter(_FILE_FORMAT))

    console_handler = RichHandler(
        console=console or Console(stderr=True),
        show_time=False,
        show_path=False,
        markup=False,
        rich_tracebacks=False,  # Tracebacks laufen so ebenfalls durch die Schwärzung
    )
    console_handler.setLevel(term_level)
    console_handler.setFormatter(RedactingFormatter("%(message)s"))

    for handler in (file_handler, console_handler):
        root.addHandler(handler)
        _installed.append(handler)
    root.setLevel(min(file_level, term_level))

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(max(logging.WARNING, root.level))
    logging.captureWarnings(True)
    return log_file
