"""Grundlagen des Plugin-Systems: Werkzeug-Definition, Registry und ``@tool``-Dekorator.

Ein Plugin ist ein Python-Modul in ``jarvis/plugins/`` mit einer oder mehreren
Funktionen, die mit ``@tool`` markiert sind::

    @tool(
        name="hello",
        description="Greets someone.",
        parameters={"type": "object", "properties": {"name": {"type": "string"}},
                    "required": ["name"]},
    )
    async def hello(args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(f"Hallo {args['name']}!")

Neue Plugins kann JARVIS über die Selbstverbesserung selbst schreiben.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any

from jarvis.router.base import ToolSpec
from jarvis.security.permissions import Level

if TYPE_CHECKING:
    from jarvis.core.app import JarvisApp

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "jarvis.plugins"


@dataclass(frozen=True)
class ImageRef:
    """Ein Bild, das in Chat und Galerie angezeigt wird."""

    url: str
    caption: str = ""
    path: str | None = None


@dataclass
class ToolResult:
    text: str
    ok: bool = True
    data: dict[str, Any] | None = None
    images: list[ImageRef] = field(default_factory=list)
    #: Enthält private Daten – danach nur noch vertrauenswürdige Modelle.
    sensitive: bool = False

    @classmethod
    def error(cls, text: str) -> ToolResult:
        return cls(text=text, ok=False)


@dataclass
class ToolContext:
    app: JarvisApp
    session_id: str | None = None
    source: str = "chat"
    project_id: str | None = None


Handler = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]
LevelRule = Level | Callable[[dict[str, Any], ToolContext], Level]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    level: LevelRule = Level.AUTO
    category: str = "allgemein"
    #: Menschlich lesbare Beschreibung für Bestätigungsdialoge.
    summary: Callable[[dict[str, Any]], str] | None = None
    #: Liefert einen Grund, warum das Werkzeug gerade nicht nutzbar ist.
    available: Callable[[ToolContext], str | None] | None = None
    timeout_s: float = 120.0

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters)

    def resolve_level(self, args: dict[str, Any], ctx: ToolContext) -> Level:
        return self.level(args, ctx) if callable(self.level) else self.level


_DECLARED: list[Tool] = []


def tool(
    *,
    name: str,
    description: str,
    parameters: dict[str, Any] | None = None,
    level: LevelRule = Level.AUTO,
    category: str = "allgemein",
    summary: Callable[[dict[str, Any]], str] | None = None,
    available: Callable[[ToolContext], str | None] | None = None,
    timeout_s: float = 120.0,
) -> Callable[[Handler], Handler]:
    """Dekorator: registriert eine async-Funktion als Werkzeug."""

    def decorator(func: Handler) -> Handler:
        _DECLARED.append(
            Tool(
                name=name,
                description=description,
                parameters=parameters or {"type": "object", "properties": {}},
                handler=func,
                level=level,
                category=category,
                summary=summary,
                available=available,
                timeout_s=timeout_s,
            )
        )
        return func

    return decorator


def obj(properties: dict[str, Any], required: Iterable[str] = ()) -> dict[str, Any]:
    """Kurzform für ein JSON-Schema-Objekt."""
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    req = list(required)
    if req:
        schema["required"] = req
    return schema


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, item: Tool) -> None:
        if item.name in self._tools and self._tools[item.name].handler is not item.handler:
            log.warning("Werkzeug '%s' wird überschrieben", item.name)
        self._tools[item.name] = item

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return sorted(self._tools.values(), key=lambda t: (t.category, t.name))

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __len__(self) -> int:
        return len(self._tools)


def load_plugins(registry: ToolRegistry, *, disabled: Iterable[str] = ()) -> ToolRegistry:
    """Importiert alle Plugin-Module und übernimmt ihre Werkzeuge in die Registry."""
    import jarvis.plugins as package

    for module in pkgutil.iter_modules(package.__path__):
        if module.name.startswith("_") or module.name in {"base", "executor"}:
            continue
        try:
            importlib.import_module(f"{package.__name__}.{module.name}")
        except Exception:
            log.exception("Plugin '%s' konnte nicht geladen werden", module.name)
    for entry_point in entry_points(group=ENTRY_POINT_GROUP):
        try:
            entry_point.load()
        except Exception:
            log.exception("Plugin-Entry-Point '%s' konnte nicht geladen werden", entry_point.name)
    blocked = set(disabled)
    for item in _DECLARED:
        if item.name not in blocked:
            registry.register(item)
    return registry
