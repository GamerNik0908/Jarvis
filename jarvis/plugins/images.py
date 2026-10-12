"""Bilder erzeugen (Pollinations, kostenlos) und im Internet suchen (Openverse, kostenlos).

Bilder landen in der Galerie und werden direkt im Chat angezeigt.
"""

from __future__ import annotations

import asyncio
import io
import logging
import random
from typing import Any
from urllib.parse import quote

import httpx

from jarvis.plugins.base import ImageRef, ToolContext, ToolResult, obj, tool

log = logging.getLogger(__name__)

POLLINATIONS_URLS = (
    "https://gen.pollinations.ai/image/{prompt}",
    "https://image.pollinations.ai/prompt/{prompt}",
)
OPENVERSE_URL = "https://api.openverse.org/v1/images/"


def _image_disabled(ctx: ToolContext) -> str | None:
    return "Bildgenerierung ist ausgeschaltet" if ctx.app.config.images.generator == "off" else None


@tool(
    name="image_generate",
    description=(
        "Create an image from a text description (free, via Pollinations). Write the prompt in "
        "English for best results. The image is shown to the user automatically."
    ),
    parameters=obj(
        {
            "prompt": {
                "type": "string",
                "description": "Detailed image description, ideally English",
            },
            "width": {"type": "integer"},
            "height": {"type": "integer"},
        },
        ["prompt"],
    ),
    category="bilder",
    available=_image_disabled,
    timeout_s=180,
)
async def image_generate(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    config = ctx.app.config.images
    prompt = str(args["prompt"]).strip()
    width = max(256, min(int(args.get("width") or config.default_width), 2048))
    height = max(256, min(int(args.get("height") or config.default_height), 2048))
    params: dict[str, Any] = {
        "width": width,
        "height": height,
        "seed": random.randint(1, 2**31 - 1),
        "nologo": "true",
    }
    if config.pollinations_model:
        params["model"] = config.pollinations_model
    key = ctx.app.secrets.get("POLLINATIONS_API_KEY")
    if key:
        params["key"] = key
    client = ctx.app.http()
    last_error = ""
    for template in POLLINATIONS_URLS:
        url = template.format(prompt=quote(prompt, safe=""))
        try:
            response = await client.get(url, params=params, timeout=150, follow_redirects=True)
        except httpx.HTTPError as exc:
            last_error = str(exc)
            continue
        content_type = response.headers.get("content-type", "")
        if response.status_code == 200 and content_type.startswith("image/"):
            saved = await asyncio.to_thread(
                ctx.app.images.save,
                response.content,
                content_type=content_type,
                kind="generated",
                prompt=prompt,
                source_url=url.split("?")[0],
                session_id=ctx.session_id,
            )
            size = _image_size(response.content) or (width, height)
            return ToolResult(
                f"Bild erstellt und angezeigt ({size[0]}×{size[1]}): {prompt}",
                images=[ImageRef(url=saved["url"], caption=prompt, path=saved["path"])],
                data=saved,
            )
        last_error = f"HTTP {response.status_code}: {response.text[:200]}"
    return ToolResult.error(f"Bild konnte nicht erzeugt werden ({last_error})")


async def _openverse(ctx: ToolContext, query: str, count: int) -> list[dict[str, Any]]:
    response = await ctx.app.http().get(
        OPENVERSE_URL, params={"q": query, "page_size": count, "mature": "false"}, timeout=20
    )
    response.raise_for_status()
    return list(response.json().get("results", []))


def _ddgs_images(query: str, count: int) -> list[dict[str, Any]]:
    from ddgs import DDGS

    with DDGS() as client:
        return [
            {
                "url": item.get("image"),
                "thumbnail": item.get("thumbnail"),
                "title": item.get("title", ""),
                "foreign_landing_url": item.get("url"),
                "creator": item.get("source", ""),
                "license": "",
            }
            for item in client.images(query, max_results=count)
        ]


@tool(
    name="image_search",
    description=(
        "Search the internet for existing photos/pictures (free licences via Openverse, otherwise "
        "web image search). The images are shown to the user automatically."
    ),
    parameters=obj(
        {
            "query": {"type": "string"},
            "count": {"type": "integer", "description": "1-8, default 4"},
        },
        ["query"],
    ),
    category="bilder",
    timeout_s=90,
)
async def image_search(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    query = str(args["query"]).strip()
    count = max(1, min(int(args.get("count") or 4), 8))
    try:
        results = await _openverse(ctx, query, count)
    except (httpx.HTTPError, ValueError) as exc:
        log.info("Openverse nicht erreichbar: %s", exc)
        results = []
    if not results:
        try:
            results = await asyncio.to_thread(_ddgs_images, query, count)
        except Exception as exc:
            return ToolResult.error(f"Bildersuche fehlgeschlagen: {exc}")
    client = ctx.app.http()
    images: list[ImageRef] = []
    lines: list[str] = []
    for item in results[:count]:
        source = item.get("thumbnail") or item.get("url")
        if not source:
            continue
        try:
            response = await client.get(source, timeout=25, follow_redirects=True)
        except httpx.HTTPError:
            continue
        content_type = response.headers.get("content-type", "")
        if response.status_code != 200 or not content_type.startswith("image/"):
            continue
        attribution = " · ".join(
            part
            for part in (
                item.get("title"),
                item.get("creator"),
                (item.get("license") or "").upper(),
            )
            if part
        )
        saved = await asyncio.to_thread(
            ctx.app.images.save,
            response.content,
            content_type=content_type,
            kind="search",
            prompt=query,
            source_url=item.get("foreign_landing_url") or item.get("url"),
            attribution=attribution,
            session_id=ctx.session_id,
        )
        images.append(ImageRef(url=saved["url"], caption=attribution or query, path=saved["path"]))
        lines.append(f"- {attribution or query} ({item.get('foreign_landing_url') or source})")
    if not images:
        return ToolResult.error(f"Keine Bilder zu '{query}' gefunden.")
    return ToolResult(
        f"{len(images)} Bilder zu '{query}' angezeigt:\n" + "\n".join(lines), images=images
    )


def _image_size(data: bytes) -> tuple[int, int] | None:
    """Tatsächliche Bildgröße (der Dienst liefert nicht immer die gewünschte)."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            return image.size
    except Exception:
        return None
