"""Eingebaute Anbieter-Plugins. Jedes Modul registriert sich per ``@register_provider``."""

from jarvis.router.providers import (
    anthropic_provider,
    claude_code_provider,
    gemini_provider,
    ollama_provider,
    openai_compat_provider,
)

__all__ = [
    "anthropic_provider",
    "claude_code_provider",
    "gemini_provider",
    "ollama_provider",
    "openai_compat_provider",
]
