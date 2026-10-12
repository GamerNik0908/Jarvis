"""Eingebaute Anbieter-Plugins. Jedes Modul registriert sich per ``@register_provider``."""

from jarvis.router.providers import anthropic_provider

__all__ = ["anthropic_provider"]
