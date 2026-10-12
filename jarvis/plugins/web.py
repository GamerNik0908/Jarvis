"""Websuche (DuckDuckGo), Webseiten lesen und Wetter (Open-Meteo) – alles ohne API-Key."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import httpx

from jarvis.plugins.base import ToolContext, ToolResult, obj, tool

log = logging.getLogger(__name__)

_MAX_PAGE_BYTES = 3 * 1024 * 1024

WEATHER_CODES = {
    0: "klar", 1: "überwiegend klar", 2: "teilweise bewölkt", 3: "bedeckt",
    45: "Nebel", 48: "Reifnebel", 51: "leichter Nieselregen", 53: "Nieselregen",
    55: "starker Nieselregen", 56: "gefrierender Nieselregen", 57: "gefrierender Nieselregen",
    61: "leichter Regen", 63: "Regen", 65: "starker Regen", 66: "gefrierender Regen",
    67: "gefrierender Regen", 71: "leichter Schneefall", 73: "Schneefall", 75: "starker Schneefall",
    77: "Schneegriesel", 80: "leichte Regenschauer", 81: "Regenschauer", 82: "heftige Regenschauer",
    85: "Schneeschauer", 86: "starke Schneeschauer", 95: "Gewitter", 96: "Gewitter mit Hagel",
    99: "schweres Gewitter mit Hagel",
}  # fmt: skip


def is_public_url(url: str) -> bool:
    """Verhindert, dass Modelle über ``web_fetch`` lokale Dienste abfragen (z. B. JARVIS selbst)."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname
    if host in {"localhost"} or host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
        ):
            return False
    return True


async def download_public(
    client: httpx.AsyncClient, url: str, *, max_redirects: int = 5
) -> tuple[bytes, str, str | None, str]:
    """Lädt eine öffentliche URL; jede Weiterleitung wird erneut geprüft (kein SSRF)."""
    current = url
    for _ in range(max_redirects + 1):
        if not await asyncio.to_thread(is_public_url, current):
            raise PermissionError(
                "Nur öffentliche Webseiten sind erlaubt (keine lokalen Adressen)."
            )
        async with client.stream("GET", current, follow_redirects=False, timeout=30) as response:
            if response.is_redirect:
                location = response.headers.get("location", "")
                current = str(response.url.join(location))
                continue
            response.raise_for_status()
            chunks = []
            size = 0
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size > _MAX_PAGE_BYTES:
                    break
            return (
                b"".join(chunks),
                response.headers.get("content-type", ""),
                response.encoding,
                str(response.url),
            )
    raise PermissionError("Zu viele Weiterleitungen")


class _PageText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.parts: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg", "nav", "footer", "header", "form"}:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in {"p", "br", "li", "h1", "h2", "h3", "h4", "tr", "div", "section", "article"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if (
            tag in {"script", "style", "noscript", "svg", "nav", "footer", "header", "form"}
            and self._skip
        ):
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    parser = _PageText()
    parser.feed(html)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return parser.title.strip(), text.strip()


def _ddgs_search(query: str, kind: str, max_results: int) -> list[dict[str, Any]]:
    from ddgs import DDGS

    with DDGS() as client:
        if kind == "news":
            return list(client.news(query, region="de-de", max_results=max_results))
        return list(
            client.text(query, region="de-de", safesearch="moderate", max_results=max_results)
        )


@tool(
    name="web_search",
    description="Search the internet (DuckDuckGo). kind='news' for current news. Returns titles, links and snippets.",
    parameters=obj(
        {
            "query": {"type": "string"},
            "kind": {"type": "string", "enum": ["web", "news"]},
            "max_results": {"type": "integer", "description": "1-10, default 6"},
        },
        ["query"],
    ),
    category="internet",
    timeout_s=45,
)
async def web_search(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    query = str(args["query"])
    kind = str(args.get("kind") or "web")
    limit = max(1, min(int(args.get("max_results") or 6), 10))
    try:
        results = await asyncio.to_thread(_ddgs_search, query, kind, limit)
    except Exception as exc:
        return ToolResult.error(f"Websuche fehlgeschlagen: {exc}")
    if not results:
        return ToolResult(f"Keine Ergebnisse für '{query}'.")
    lines = []
    for index, item in enumerate(results, 1):
        title = item.get("title", "")
        link = item.get("href") or item.get("url") or ""
        body = (item.get("body") or item.get("excerpt") or "").strip()
        date = item.get("date", "")
        lines.append(f"{index}. {title}{f' ({date})' if date else ''}\n   {link}\n   {body}")
    return ToolResult("\n".join(lines), data={"results": results})


@tool(
    name="web_fetch",
    description="Download a public web page and return its readable text (for reading articles, docs, recipes …).",
    parameters=obj(
        {
            "url": {"type": "string"},
            "max_chars": {"type": "integer", "description": "Default 15000"},
        },
        ["url"],
    ),
    category="internet",
    timeout_s=60,
)
async def web_fetch(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    url = str(args["url"]).strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if not await asyncio.to_thread(is_public_url, url):
        return ToolResult.error("Nur öffentliche Webseiten sind erlaubt (keine lokalen Adressen).")
    limit = min(int(args.get("max_chars") or 15_000), 60_000)
    try:
        raw, content_type, encoding, final_url = await download_public(ctx.app.http(), url)
    except PermissionError as exc:
        return ToolResult.error(str(exc))
    except httpx.HTTPError as exc:
        return ToolResult.error(f"Seite nicht abrufbar: {exc}")
    url = final_url
    text = raw.decode(encoding or "utf-8", "replace")
    if "html" in content_type or text.lstrip().startswith("<"):
        title, body = html_to_text(text)
    else:
        title, body = "", text
    suffix = "\n… (gekürzt)" if len(body) > limit else ""
    return ToolResult(f"{title}\n{url}\n\n{body[:limit]}{suffix}".strip())


async def fetch_weather(ctx: ToolContext, location: str) -> dict[str, Any]:
    client = ctx.app.http()
    geo = await client.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": location, "count": 1, "language": "de", "format": "json"},
        timeout=15,
    )
    geo.raise_for_status()
    places = geo.json().get("results") or []
    if not places:
        raise ValueError(f"Ort '{location}' nicht gefunden")
    place = places[0]
    forecast = await client.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": "auto",
            "forecast_days": 4,
        },
        timeout=15,
    )
    forecast.raise_for_status()
    data: dict[str, Any] = forecast.json()
    data["place"] = f"{place.get('name')}, {place.get('country', '')}".strip(", ")
    return data


def format_weather(data: dict[str, Any]) -> str:
    current = data.get("current", {})
    lines = [
        f"Wetter in {data.get('place')}: {WEATHER_CODES.get(int(current.get('weather_code', -1)), 'unbekannt')}, "
        f"{current.get('temperature_2m')} °C (gefühlt {current.get('apparent_temperature')} °C), "
        f"Luftfeuchte {current.get('relative_humidity_2m')} %, Wind {current.get('wind_speed_10m')} km/h"
    ]
    daily = data.get("daily", {})
    days = daily.get("time", [])
    for index, day in enumerate(days):
        lines.append(
            f"{day}: {WEATHER_CODES.get(int(daily['weather_code'][index]), '?')}, "
            f"{daily['temperature_2m_min'][index]}–{daily['temperature_2m_max'][index]} °C, "
            f"Regenrisiko {daily['precipitation_probability_max'][index]} %"
        )
    return "\n".join(lines)


@tool(
    name="weather",
    description="Current weather and 4-day forecast for a place. Without location: the user's home location.",
    parameters=obj({"location": {"type": "string"}}),
    category="internet",
    timeout_s=40,
)
async def weather(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    location = str(args.get("location") or ctx.app.config.assistant.home_location or "").strip()
    if not location:
        return ToolResult.error(
            "Kein Ort angegeben. Tipp: Wohnort in den Einstellungen (assistant.home_location) setzen."
        )
    try:
        data = await fetch_weather(ctx, location)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        return ToolResult.error(f"Wetter nicht abrufbar: {exc}")
    return ToolResult(format_weather(data), data=data)
