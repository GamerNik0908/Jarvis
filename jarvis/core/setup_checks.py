"""Einrichtungs-Checkliste für Dashboard und ``jarvis setup``: Was ist bereit, was fehlt noch?"""

from __future__ import annotations

import importlib.util
import shutil
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from jarvis.router.providers.claude_code_provider import ClaudeCodeProvider
from jarvis.router.providers.gemini_provider import GeminiProvider
from jarvis.router.providers.ollama_provider import OllamaProvider, model_matches

if TYPE_CHECKING:
    from jarvis.core.app import JarvisApp


@dataclass
class CheckItem:
    key: str
    title: str
    ok: bool
    detail: str
    hint: str = ""
    optional: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def missing_ollama_models(app: JarvisApp) -> list[str]:
    ollama = app.provider_of_type(OllamaProvider)
    if ollama is None or ollama._installed is None:
        return []
    wanted = {
        m.target for m in app.router.models() if m.provider == ollama.instance_name and m.enabled
    }
    if app.recommendation is not None and app.config.memory.embedder in ("auto", "ollama"):
        wanted.add(app.recommendation.embedding)
    return sorted(name for name in wanted if not model_matches(ollama.installed_models, name))


def run_checks(app: JarvisApp) -> list[CheckItem]:
    items: list[CheckItem] = []
    claude = app.provider_of_type(ClaudeCodeProvider)
    if claude is not None:
        reason = claude.unavailable_reason()
        items.append(
            CheckItem(
                "claude_code",
                "Claude (über dein Pro-Abo)",
                reason is None,
                "bereit" if reason is None else reason,
                hint="winget install Anthropic.ClaudeCode – danach 'claude' starten und anmelden",
            )
        )
    gemini = app.provider_of_type(GeminiProvider)
    if gemini is not None:
        key = gemini.api_key()
        items.append(
            CheckItem(
                "gemini",
                "Gemini (kostenloser API-Key)",
                bool(key),
                "Key hinterlegt" if key else "kein Key",
                hint="Key kostenlos unter https://aistudio.google.com/apikey erstellen und hier einfügen",
                optional=True,
            )
        )
    ollama = app.provider_of_type(OllamaProvider)
    if ollama is not None:
        reason = ollama.unavailable_reason()
        missing = missing_ollama_models(app)
        detail = reason or (
            "Modelle fehlen: " + ", ".join(missing) if missing else "läuft, Modelle bereit"
        )
        items.append(
            CheckItem(
                "ollama",
                "Lokale KI (Ollama, offline & privat)",
                reason is None and not missing,
                detail,
                hint="winget install Ollama.Ollama – fehlende Modelle lädt JARVIS per Knopfdruck",
                optional=True,
            )
        )
    google = app.google
    accounts = google.accounts()
    items.append(
        CheckItem(
            "google",
            "Gmail & Kalender",
            bool(accounts),
            ", ".join(accounts)
            if accounts
            else ("OAuth-Client fehlt" if not google.configured() else "kein Konto verbunden"),
            hint="Anleitung im README (Abschnitt Gmail & Kalender), dann 'jarvis google add'",
            optional=True,
        )
    )
    voice_ok = all(_module(m) for m in ("faster_whisper", "sounddevice", "openwakeword"))
    items.append(
        CheckItem(
            "voice",
            "Sprachsteuerung",
            voice_ok and app.config.voice.enabled,
            "Pakete vorhanden" if voice_ok else "Sprachpakete fehlen",
            hint="Installationsskript erneut ausführen",
            optional=True,
        )
    )
    items.append(
        CheckItem(
            "git",
            "Git (für Selbstverbesserung)",
            shutil.which("git") is not None,
            "installiert" if shutil.which("git") else "fehlt",
            hint="winget install Git.Git",
        )
    )
    if app.improvements is not None:
        reason = app.improvements.unavailable_reason()
        items.append(
            CheckItem(
                "self_improve",
                "Selbstverbesserung mit Claude Code",
                reason is None,
                "bereit" if reason is None else reason,
            )
        )
    return items
