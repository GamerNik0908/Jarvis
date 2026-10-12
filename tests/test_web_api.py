"""Web-Dashboard und API: Token, Host-/Origin-Prüfung, alle Endpunkte und Chat-Streaming.

Die meisten Tests laufen mit ``httpx.AsyncClient`` direkt gegen die ASGI-App –
so teilen sich Test, API und Hintergrund-Tasks (Chat, Bestätigungen) eine
Ereignisschleife. Für den WebSocket wird Starlettes ``TestClient`` benutzt.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jarvis import __version__
from jarvis.core.app import JarvisApp
from jarvis.core.chat import ChatService
from jarvis.core.config import load_config
from jarvis.core.events import Event, Subscription
from jarvis.core.timeutil import now
from jarvis.self_improve.manager import ImprovementManager
from jarvis.ui.web.server import create_web_app, load_or_create_token

from .conftest import FakeProvider

PORT = 8765
TOKEN = "0123456789abcdef" * 3
BASE = f"http://127.0.0.1:{PORT}"
AUTH = {"X-Jarvis-Token": TOKEN}
WS_URL = f"ws://127.0.0.1:{PORT}/ws?t={TOKEN}"
ALLOWED_SECRETS = (
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "POLLINATIONS_API_KEY",
)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


# ---------------------------------------------------------------------- Fixtures


@pytest.fixture
def app(config_file: Path) -> Iterator[JarvisApp]:
    application = JarvisApp.bootstrap(config_file, role="test", detect=False)
    yield application
    application.db.close()


@pytest.fixture
def fake(app: JarvisApp) -> FakeProvider:
    provider = app.router.providers["fake"]
    assert isinstance(provider, FakeProvider)
    return provider


@pytest.fixture
def chat(app: JarvisApp) -> ChatService:
    return ChatService(app)


@pytest.fixture
def web(app: JarvisApp, chat: ChatService) -> FastAPI:
    return create_web_app(app, chat, token=TOKEN, port=PORT)


def make_client(web: FastAPI, headers: dict[str, str] | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web), base_url=BASE, headers=headers or {}
    )


@pytest.fixture
async def client(web: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Client mit gültigem Token im Header."""
    async with make_client(web, AUTH) as http:
        yield http


@pytest.fixture
async def anon(web: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Client ohne Token."""
    async with make_client(web) as http:
        yield http


@pytest.fixture
def sync_client(web: FastAPI) -> Iterator[TestClient]:
    """Für WebSocket-Tests; die Ereignisschleife läuft im Thread des TestClient."""
    with TestClient(web, base_url=BASE) as http:
        yield http


@pytest.fixture
def keyring_store(app: JarvisApp, monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    """Ersetzt den Windows Credential Manager durch ein Wörterbuch."""
    store: dict[tuple[str, str], str] = {}
    for name in ALLOWED_SECRETS:
        monkeypatch.delenv(name, raising=False)

    def set_password(service: str, name: str, value: str) -> None:
        store[(service, name)] = value

    def get_password(service: str, name: str) -> str | None:
        return store.get((service, name))

    monkeypatch.setattr("keyring.set_password", set_password)
    monkeypatch.setattr("keyring.get_password", get_password)
    monkeypatch.setattr(app.secrets, "_use_keyring", True)
    return store


# ---------------------------------------------------------------------- Hilfen


async def until(sub: Subscription, event_type: str, timeout: float = 5.0) -> list[Event]:
    """Sammelt Ereignisse, bis eines vom gewünschten Typ ankommt."""
    events: list[Event] = []
    async with asyncio.timeout(timeout):
        while True:
            event = await sub.get()
            assert event is not None
            events.append(event)
            if event.type == event_type:
                return events


async def wait_idle(chat: ChatService, conversation_id: str) -> None:
    for _ in range(200):
        if not chat.busy(conversation_id):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Chat-Lauf endet nicht")


async def wait_pending(client: httpx.AsyncClient, count: int = 1) -> list[dict[str, Any]]:
    """Wartet, bis die API offene Bestätigungen meldet."""
    for _ in range(200):
        items: list[dict[str, Any]] = (await client.get("/api/confirmations")).json()
        if len(items) >= count:
            return items
        await asyncio.sleep(0.01)
    raise AssertionError("keine offene Bestätigung")


def local_config(app: JarvisApp) -> Path:
    return app.loaded.path.with_name("config.local.yaml")


# ---------------------------------------------------------------------- Seite & statische Dateien


async def test_index_replaces_placeholders(anon: httpx.AsyncClient) -> None:
    response = await anon.get("/")
    assert response.status_code == 200
    html = response.text
    assert "__JARVIS_TOKEN__" not in html
    assert "__JARVIS_VERSION__" not in html
    assert f'token: "{TOKEN}"' in html
    assert f"/static/app.js?v={__version__}" in html
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize(
    ("path", "mime"),
    [
        ("/static/app.js", "javascript"),
        ("/static/style.css", "text/css"),
        ("/static/icon.svg", "image/svg+xml"),
    ],
)
async def test_static_files_need_no_token(anon: httpx.AsyncClient, path: str, mime: str) -> None:
    response = await anon.get(path)
    assert response.status_code == 200
    assert mime in response.headers["content-type"]


async def test_print_button_assets(anon: httpx.AsyncClient) -> None:
    """Drucken-Knopf: Skript liefert Knopf + Druckbereich, Stylesheet blendet beim Drucken alles andere aus."""
    script = (await anon.get("/static/app.js")).text
    assert "function printButton" in script
    assert "function printMessage" in script
    assert "window.print()" in script
    assert "print: '<path" in script  # Symbol ist definiert
    css = (await anon.get("/static/style.css")).text
    assert "@media print" in css
    assert "body.printing > :not(#print-root)" in css
    assert "#print-root { display: none; }" in css


@pytest.mark.parametrize("path", ["/static/..%2Fserver.py", "/static/%2E%2E/server.py"])
async def test_static_traversal_blocked(anon: httpx.AsyncClient, path: str) -> None:
    response = await anon.get(path)
    assert response.status_code == 404
    assert "create_web_app" not in response.text


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_api_docs_are_disabled(anon: httpx.AsyncClient, path: str) -> None:
    assert (await anon.get(path)).status_code == 404


# ---------------------------------------------------------------------- Token & Host


async def test_api_requires_token(anon: httpx.AsyncClient) -> None:
    response = await anon.get("/api/status")
    assert response.status_code == 401
    assert response.json() == {"detail": "Token fehlt oder ist falsch"}


@pytest.mark.parametrize("wrong", ["falsch", TOKEN[:-1], TOKEN + "0", TOKEN.upper()])
async def test_wrong_token_rejected(anon: httpx.AsyncClient, wrong: str) -> None:
    assert (await anon.get("/api/status", headers={"X-Jarvis-Token": wrong})).status_code == 401
    assert (await anon.get("/api/status", params={"t": wrong})).status_code == 401


async def test_non_ascii_token_is_401_not_500(anon: httpx.AsyncClient) -> None:
    """compare_digest wirft bei Nicht-ASCII-Strings – das darf kein Serverfehler werden."""
    by_header = await anon.get("/api/status", headers={"X-Jarvis-Token": "ä".encode()})
    assert by_header.status_code == 401
    assert (await anon.get("/api/status", params={"t": "Schlüssel"})).status_code == 401


async def test_token_via_header_or_query(anon: httpx.AsyncClient) -> None:
    assert (await anon.get("/api/status", headers=AUTH)).status_code == 200
    assert (await anon.get("/api/status", params={"t": TOKEN})).status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "testserver",
        "evil.example",
        f"evil.example:{PORT}",
        f"127.0.0.1:{PORT + 1}",
        "127.0.0.1",
        "localhost",
        f"0.0.0.0:{PORT}",
    ],
)
async def test_foreign_host_rejected(anon: httpx.AsyncClient, host: str) -> None:
    """Schutz vor DNS-Rebinding: fremde Host-Header bekommen weder Seite noch API."""
    for path in ("/", "/api/status", "/static/app.js"):
        response = await anon.get(path, headers={"Host": host, **AUTH})
        assert response.status_code == 403, path
        assert response.json() == {"detail": "Host nicht erlaubt"}
        assert TOKEN not in response.text


async def test_localhost_host_allowed(anon: httpx.AsyncClient) -> None:
    response = await anon.get("/api/status", headers={"Host": f"localhost:{PORT}", **AUTH})
    assert response.status_code == 200


def test_load_or_create_token(tmp_path: Path) -> None:
    token = load_or_create_token(tmp_path)
    assert len(token) == 48
    assert load_or_create_token(tmp_path) == token  # bleibt stabil
    (tmp_path / "api_token").write_text("zu-kurz", encoding="utf-8")
    renewed = load_or_create_token(tmp_path)
    assert renewed != "zu-kurz"
    assert len(renewed) == 48


# ---------------------------------------------------------------------- Status & Übersicht


async def test_status(client: httpx.AsyncClient) -> None:
    data = (await client.get("/api/status")).json()
    assert data["version"] == __version__
    assert data["name"] == "JARVIS"
    assert set(data["models_ready"]) == {"fake-big", "fake-small", "fake-free"}
    assert data["killswitch"] == {"engaged": False, "reason": ""}
    assert data["voice"] == {"enabled": False, "state": "aus"}
    assert data["confirmations"] == []
    assert data["hardware"] is None
    assert isinstance(data["budget"], str)


async def test_overview(app: JarvisApp, client: httpx.AsyncClient) -> None:
    app.cards.upsert("Einkauf", "- Milch")
    app.reminders.create("Tee trinken", now() + timedelta(hours=1))
    data = (await client.get("/api/overview")).json()
    assert {"system", "cards", "reminders", "mail", "calendar", "costs"} <= set(data)
    assert data["cards"][0]["title"] == "Einkauf"
    assert data["reminders"][0]["text"] == "Tee trinken"
    assert data["mail"] == []
    assert data["calendar"] == []
    assert data["projects"] == []
    assert data["improvements"] == []
    assert data["suggestions"] == []
    assert data["images"] == []
    assert data["confirmations"] == []
    assert "cpu_percent" in data["system"]
    assert (await client.post("/api/overview/refresh")).json() == {"ok": True}


async def test_system_weather_and_setup(client: httpx.AsyncClient) -> None:
    assert "ram_total_gb" in (await client.get("/api/system")).json()
    # Kein Wohnort konfiguriert → leeres Wetter ohne Netzwerkzugriff.
    assert (await client.get("/api/weather")).json() == {}
    setup = (await client.get("/api/setup")).json()
    assert {"google", "voice", "git"} <= {check["key"] for check in setup["checks"]}
    assert setup["missing_models"] == []
    pull = await client.post("/api/setup/ollama-pull")
    assert pull.status_code == 404


async def test_models(client: httpx.AsyncClient) -> None:
    data = (await client.get("/api/models")).json()
    models = {m["id"]: m for m in data["models"]}
    assert set(models) == {"fake-big", "fake-small", "fake-free", "fake-pricey"}
    assert models["fake-big"]["status"] == "bereit"
    assert models["fake-big"]["paid"] is True
    assert models["fake-big"]["tools"] is True
    assert models["fake-free"]["local"] is True
    assert models["fake-free"]["paid"] is False
    assert models["fake-pricey"]["status"] != "bereit"
    assert data["routes"]["chat"] == ["fake-big", "fake-small"]
    assert data["routes"]["code"] == ["fake-big"]


async def test_costs_empty(client: httpx.AsyncClient) -> None:
    data = (await client.get("/api/costs")).json()
    assert data["day"] == []
    assert data["month"] == []
    assert isinstance(data["status"], str)


async def test_logs(app: JarvisApp, client: httpx.AsyncClient) -> None:
    data = (await client.get("/api/logs")).json()
    assert data["file"] == str(app.log_file)
    assert any("startet (test)" in line for line in data["lines"])
    assert len((await client.get("/api/logs", params={"lines": 1})).json()["lines"]) == 1
    # 0 heißt "nichts" – nicht "die ganze Datei".
    assert (await client.get("/api/logs", params={"lines": 0})).json()["lines"] == []
    assert (await client.get("/api/logs", params={"lines": -5})).json()["lines"] == []


# ---------------------------------------------------------------------- Chat


async def test_chat_streams_answer_and_stores_conversation(
    app: JarvisApp, chat: ChatService, client: httpx.AsyncClient
) -> None:
    with app.events.subscribe(prefixes=("chat.",)) as sub:
        response = await client.post("/api/chat", json={"text": "Wie geht's?"})
        assert response.status_code == 200
        conversation = response.json()["conversation_id"]
        events = await until(sub, "chat.final")
    assert all(e.session_id == conversation for e in events)
    assert events[0].type == "chat.started"
    assert "".join(e.data["text"] for e in events if e.type == "chat.delta") == "Hallo Welt"
    final = events[-1].data
    assert final["text"] == "Hallo Welt"
    assert final["model"] == "fake-big"
    assert final["cost_usd"] > 0

    await wait_idle(chat, conversation)
    detail = (await client.get(f"/api/conversations/{conversation}")).json()
    assert detail["busy"] is False
    assert [(m["role"], m["content"]) for m in detail["messages"]] == [
        ("user", "Wie geht's?"),
        ("assistant", "Hallo Welt"),
    ]
    assert detail["messages"][0]["meta"]["source"] == "web"
    listing = (await client.get("/api/conversations")).json()
    assert listing[0]["id"] == conversation
    assert listing[0]["title"] == "Wie geht's?"

    costs = (await client.get("/api/costs")).json()
    assert [(r["model"], r["calls"], r["input_tokens"]) for r in costs["day"]] == [
        ("fake-big", 1, 1000)
    ]


async def test_chat_continues_conversation_with_pinned_model(
    app: JarvisApp, chat: ChatService, client: httpx.AsyncClient, fake: FakeProvider
) -> None:
    conversation = app.conversations.create()
    with app.events.subscribe(types={"chat.final"}) as sub:
        response = await client.post(
            "/api/chat",
            json={"text": "Kurz bitte", "conversation_id": conversation, "model": "fake-small"},
        )
        assert response.json() == {"conversation_id": conversation}
        (final,) = await until(sub, "chat.final")
    assert final.data["model"] == "fake-small"
    assert fake.requests[-1].model == "fake-small"
    await wait_idle(chat, conversation)


async def test_chat_task_kind_selects_route(
    app: JarvisApp, chat: ChatService, client: httpx.AsyncClient, fake: FakeProvider
) -> None:
    with app.events.subscribe(types={"chat.final"}) as sub:
        response = await client.post("/api/chat", json={"text": "2+2?", "task": "simple"})
        conversation = response.json()["conversation_id"]
        await until(sub, "chat.final")
    assert fake.requests[-1].model == "fake-small"  # Route "simple" beginnt mit fake-small
    await wait_idle(chat, conversation)


async def test_chat_rejects_empty_text_and_unknown_task(
    app: JarvisApp, client: httpx.AsyncClient
) -> None:
    assert (await client.post("/api/chat", json={"text": "   "})).status_code == 400
    bad = await client.post("/api/chat", json={"text": "Hallo", "task": "zaubern"})
    assert bad.status_code == 400
    # Ungültige Anfragen dürfen keine leeren Gespräche hinterlassen.
    assert app.conversations.list() == []
    assert (await client.post("/api/chat", json={})).status_code == 422


async def test_chat_busy_and_cancel(
    app: JarvisApp, chat: ChatService, client: httpx.AsyncClient, fake: FakeProvider
) -> None:
    fake.delays["fake-big"] = 0.5
    with app.events.subscribe(types={"chat.started", "chat.cancelled"}) as sub:
        response = await client.post("/api/chat", json={"text": "Erzähl was Langes"})
        conversation = response.json()["conversation_id"]
        await until(sub, "chat.started")
        second = await client.post(
            "/api/chat", json={"text": "Noch was", "conversation_id": conversation}
        )
        assert second.status_code == 409
        assert (await client.get(f"/api/conversations/{conversation}")).json()["busy"] is True
        cancel = await client.post(f"/api/chat/{conversation}/cancel")
        assert cancel.json() == {"cancelled": True}
        (cancelled,) = await until(sub, "chat.cancelled", timeout=2)
    assert cancelled.session_id == conversation
    await wait_idle(chat, conversation)
    assert (await client.post(f"/api/chat/{conversation}/cancel")).json() == {"cancelled": False}


async def test_chat_error_is_published(
    app: JarvisApp, chat: ChatService, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("kaputt")

    monkeypatch.setattr(app.agent, "ask", broken)
    with app.events.subscribe(types={"chat.error"}) as sub:
        response = await client.post("/api/chat", json={"text": "Hallo"})
        (error,) = await until(sub, "chat.error")
    assert error.data == {"error": "Interner Fehler: kaputt"}
    assert error.session_id == response.json()["conversation_id"]
    await wait_idle(chat, response.json()["conversation_id"])


async def test_conversation_detail_and_delete(app: JarvisApp, client: httpx.AsyncClient) -> None:
    conversation = app.conversations.create()
    app.conversations.add(conversation, "user", "Hallo")
    detail = (await client.get(f"/api/conversations/{conversation}")).json()
    assert detail["id"] == conversation
    assert [m["content"] for m in detail["messages"]] == ["Hallo"]
    assert (await client.delete(f"/api/conversations/{conversation}")).json() == {"ok": True}
    assert (await client.get(f"/api/conversations/{conversation}")).status_code == 404
    assert (await client.get("/api/conversations")).json() == []
    assert (await client.get("/api/conversations/gibtsnicht")).status_code == 404


# ---------------------------------------------------------------------- Bestätigungen & Not-Aus


@pytest.mark.parametrize(("approved", "decision"), [(True, "approved"), (False, "denied")])
async def test_confirmation_resolved_via_api(
    app: JarvisApp, client: httpx.AsyncClient, approved: bool, decision: str
) -> None:
    pending = asyncio.create_task(
        app.broker.request(tool="files_delete", summary="Datei löschen: notiz.txt", source="web")
    )
    (item,) = await wait_pending(client)
    assert item["tool"] == "files_delete"
    assert item["summary"] == "Datei löschen: notiz.txt"
    assert (await client.get("/api/status")).json()["confirmations"][0]["id"] == item["id"]
    response = await client.post(f"/api/confirmations/{item['id']}", json={"approved": approved})
    assert response.json() == {"ok": True}
    assert await asyncio.wait_for(pending, 2) == (approved, decision)
    assert (await client.get("/api/confirmations")).json() == []


async def test_unknown_confirmation(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/confirmations/gibtsnicht", json={"approved": True})
    assert response.json() == {"ok": False}
    assert (await client.post("/api/confirmations/x", json={})).status_code == 422


async def test_killswitch_engage_denies_and_blocks(
    app: JarvisApp, client: httpx.AsyncClient
) -> None:
    pending = asyncio.create_task(app.broker.request(tool="shell_run", summary="dir"))
    await wait_pending(client)
    assert (await client.post("/api/killswitch", json={"engage": True})).json() == {"engaged": True}
    assert await asyncio.wait_for(pending, 2) == (False, "denied")
    status = (await client.get("/api/status")).json()
    assert status["killswitch"] == {"engaged": True, "reason": "Dashboard"}
    blocked = await client.post(
        "/api/tools/call",
        json={"name": "dashboard_card", "arguments": {"title": "A", "content": "B"}},
    )
    assert blocked.json()["ok"] is False
    assert "Not-Aus" in blocked.json()["text"]
    assert app.cards.list() == []

    assert (await client.post("/api/killswitch", json={"engage": False})).json() == {
        "engaged": False
    }
    assert (await client.get("/api/status")).json()["killswitch"] == {
        "engaged": False,
        "reason": "",
    }


# ---------------------------------------------------------------------- Karten, Erinnerungen, Gedächtnis


async def test_cards_created_by_tool_listed_and_deleted(
    app: JarvisApp, client: httpx.AsyncClient
) -> None:
    created = await client.post(
        "/api/tools/call",
        json={"name": "dashboard_card", "arguments": {"title": "Einkauf", "content": "- Milch"}},
    )
    assert created.json() == {
        "ok": True,
        "text": "Karte 'Einkauf' ist auf der Übersicht.",
        "images": [],
    }
    cards = (await client.get("/api/cards")).json()
    assert [(c["title"], c["content"]) for c in cards] == [("Einkauf", "- Milch")]
    card_id = cards[0]["id"]
    assert (await client.delete(f"/api/cards/{card_id}")).json() == {"ok": True}
    assert (await client.get("/api/cards")).json() == []
    assert (await client.delete(f"/api/cards/{card_id}")).json() == {"ok": False}


async def test_reminders_crud(client: httpx.AsyncClient) -> None:
    due = (now() + timedelta(days=1)).replace(second=0, microsecond=0)
    created = await client.post(
        "/api/reminders",
        json={"text": "Müll rausbringen", "due": due.isoformat(), "repeat": "weekly"},
    )
    assert created.status_code == 200
    reminder = created.json()
    assert reminder["text"] == "Müll rausbringen"
    assert reminder["repeat"] == "weekly"
    assert reminder["due"] == due.isoformat(timespec="minutes")

    listing = (await client.get("/api/reminders")).json()
    assert [r["id"] for r in listing] == [reminder["id"]]
    assert (await client.delete(f"/api/reminders/{reminder['id']}")).json() == {"ok": True}
    assert (await client.get("/api/reminders")).json() == []
    assert (await client.delete(f"/api/reminders/{reminder['id']}")).json() == {"ok": False}


@pytest.mark.parametrize(
    "body",
    [
        {"text": "x", "due": "morgen früh"},
        {"text": "x", "due": "2026-10-13T08:00", "repeat": "stündlich"},
    ],
)
async def test_reminder_invalid_input(client: httpx.AsyncClient, body: dict[str, Any]) -> None:
    response = await client.post("/api/reminders", json=body)
    assert response.status_code == 400
    assert (await client.get("/api/reminders")).json() == []


async def test_reminder_local_time_without_zone(client: httpx.AsyncClient) -> None:
    """``datetime-local`` aus dem Browser hat keine Zeitzone – gilt als lokale Zeit."""
    response = await client.post(
        "/api/reminders", json={"text": "Tee", "due": "2030-01-02T08:30", "repeat": None}
    )
    assert response.status_code == 200
    assert response.json()["due"].startswith("2030-01-02T08:30")


async def test_memory_list_and_delete(app: JarvisApp, client: httpx.AsyncClient) -> None:
    remembered = await client.post(
        "/api/tools/call",
        json={
            "name": "memory_remember",
            "arguments": {"fact": "Nik trinkt Kaffee schwarz", "category": "vorliebe"},
        },
    )
    assert remembered.json()["ok"] is True
    items = (await client.get("/api/memory")).json()
    assert [(m["text"], m["category"]) for m in items] == [
        ("Nik trinkt Kaffee schwarz", "vorliebe")
    ]
    memory_id = items[0]["id"]
    assert (await client.delete(f"/api/memory/{memory_id}")).json() == {"ok": True}
    assert (await client.get("/api/memory")).json() == []
    assert (await client.delete(f"/api/memory/{memory_id}")).json() == {"ok": False}


# ---------------------------------------------------------------------- Bilder


async def test_images_list_serve_and_delete(
    app: JarvisApp, client: httpx.AsyncClient, anon: httpx.AsyncClient
) -> None:
    saved = app.images.save(PNG, content_type="image/png", kind="generated", prompt="Leuchtturm")
    listing = (await client.get("/api/images")).json()
    assert [i["id"] for i in listing] == [saved["id"]]
    url = listing[0]["url"]
    assert url == f"/api/images/file/{saved['file']}"

    # So lädt das Dashboard <img>-Tags: Token als ?t=.
    image = await anon.get(url, params={"t": TOKEN})
    assert image.status_code == 200
    assert image.content == PNG
    assert image.headers["content-type"] == "image/png"
    assert (await anon.get(url)).status_code == 401

    assert (await client.delete(f"/api/images/{saved['id']}")).json() == {"ok": True}
    assert not Path(saved["path"]).exists()
    assert (await client.get(url)).status_code == 404
    assert (await client.delete(f"/api/images/{saved['id']}")).json() == {"ok": False}


@pytest.mark.parametrize(
    "name", ["..%2Fgeheim.txt", "..%5Cgeheim.txt", "%2E%2E%2Fgeheim.txt", ".versteckt", "fehlt.png"]
)
async def test_image_file_rejects_traversal(
    app: JarvisApp, client: httpx.AsyncClient, name: str
) -> None:
    (app.images.directory.parent / "geheim.txt").write_text("streng geheim", encoding="utf-8")
    (app.images.directory / ".versteckt").write_text("streng geheim", encoding="utf-8")
    response = await client.get(f"/api/images/file/{name}")
    assert response.status_code == 404
    assert "streng geheim" not in response.text


# ---------------------------------------------------------------------- Protokoll & Werkzeuge


async def test_audit_lists_tool_calls(client: httpx.AsyncClient) -> None:
    await client.post(
        "/api/tools/call",
        json={
            "name": "dashboard_card",
            "arguments": {"title": "A", "content": "B"},
            "source": "claude_code",
        },
    )
    rows = (await client.get("/api/audit")).json()
    assert len(rows) == 1
    assert rows[0]["tool"] == "dashboard_card"
    assert rows[0]["source"] == "claude_code"
    assert rows[0]["decision"] == "auto"
    assert rows[0]["ok"] == 1
    assert (await client.get("/api/audit", params={"limit": 0})).json() == []
    # Negative Grenze wäre bei SQLite "unbegrenzt".
    assert (await client.get("/api/audit", params={"limit": -1})).json() == []


async def test_tools_list_and_filter(app: JarvisApp, client: httpx.AsyncClient) -> None:
    tools = (await client.get("/api/tools")).json()["tools"]
    names = {t["name"] for t in tools}
    assert {"dashboard_card", "memory_remember", "reminder_create", "notify_user"} <= names
    card = next(t for t in tools if t["name"] == "dashboard_card")
    assert card["category"] == "übersicht"
    assert card["parameters"]["required"] == ["title", "content"]
    assert card["description"]

    filtered = (
        await client.get("/api/tools", params={"names": "dashboard_card,notify_user"})
    ).json()
    assert sorted(t["name"] for t in filtered["tools"]) == ["dashboard_card", "notify_user"]

    app.config.security.tool_levels["notify_user"] = "forbidden"
    tools = (await client.get("/api/tools")).json()["tools"]
    assert "notify_user" not in {t["name"] for t in tools}


async def test_tool_call_errors(app: JarvisApp, client: httpx.AsyncClient) -> None:
    unknown = (await client.post("/api/tools/call", json={"name": "gibtsnicht"})).json()
    assert unknown["ok"] is False
    assert "Unbekanntes Werkzeug" in unknown["text"]

    missing = await client.post(
        "/api/tools/call", json={"name": "dashboard_card", "arguments": {"title": "A"}}
    )
    assert missing.json()["ok"] is False
    assert "Pflichtfeld 'content'" in missing.json()["text"]

    app.config.security.tool_levels["notify_user"] = "forbidden"
    forbidden = await client.post(
        "/api/tools/call", json={"name": "notify_user", "arguments": {"message": "Hi"}}
    )
    assert forbidden.json()["ok"] is False
    assert "nicht erlaubt" in forbidden.json()["text"]
    assert app.audit.recent(1)[0]["decision"] == "forbidden"


@pytest.mark.parametrize(("approved", "decision"), [(True, "approved"), (False, "denied")])
async def test_tool_call_waits_for_confirmation(
    app: JarvisApp, client: httpx.AsyncClient, approved: bool, decision: str
) -> None:
    app.config.security.tool_levels["dashboard_card"] = "confirm"
    arguments = {"title": "Einkauf", "content": "- Brot"}
    call = asyncio.create_task(
        client.post(
            "/api/tools/call",
            json={"name": "dashboard_card", "arguments": arguments, "source": "claude_code"},
        )
    )
    (item,) = await wait_pending(client)
    assert item["tool"] == "dashboard_card"
    assert item["source"] == "claude_code"
    assert item["details"] == arguments
    assert app.cards.list() == []  # wartet noch auf die Zustimmung

    resolved = await client.post(f"/api/confirmations/{item['id']}", json={"approved": approved})
    assert resolved.json() == {"ok": True}
    result = (await asyncio.wait_for(call, 5)).json()
    assert result["ok"] is approved
    assert [c["title"] for c in app.cards.list()] == (["Einkauf"] if approved else [])
    if not approved:
        assert result["text"] == "Der Benutzer hat die Aktion abgelehnt."
    entry = app.audit.recent(1)[0]
    assert (entry["level"], entry["decision"]) == ("confirm", decision)


async def test_tool_call_marks_conversation_sensitive(
    app: JarvisApp, client: httpx.AsyncClient
) -> None:
    conversation = app.conversations.create()
    await app.memory.remember("Nik trinkt Kaffee schwarz")
    response = await client.post(
        "/api/tools/call",
        json={
            "name": "memory_recall",
            "arguments": {"query": "Kaffee"},
            "session_id": conversation,
        },
    )
    assert response.json()["ok"] is True
    assert "Kaffee" in response.json()["text"]
    assert app.conversations.is_sensitive(conversation)


# ---------------------------------------------------------------------- Einstellungen & Keys


async def test_settings_get(app: JarvisApp, client: httpx.AsyncClient) -> None:
    data = (await client.get("/api/settings")).json()
    assert "system_prompt" not in data["assistant"]
    assert data["assistant"]["name"] == "JARVIS"
    assert data["budget"]["daily_limit_usd"] == 1.0
    assert {"voice", "self_improve", "projects", "images", "desktop"} <= set(data)
    assert data["config_file"] == str(app.loaded.path)
    assert data["local_file"] == str(local_config(app))
    assert data["secrets"] == dict.fromkeys(ALLOWED_SECRETS)


async def test_settings_update_persists_to_local_file(
    app: JarvisApp, client: httpx.AsyncClient
) -> None:
    original = app.loaded.path.read_text(encoding="utf-8")
    response = await client.post(
        "/api/settings",
        json={"changes": {"assistant": {"user_name": "Nik"}, "voice": {"enabled": False}}},
    )
    assert response.json() == {"ok": True, "file": str(local_config(app)), "restart_needed": True}
    second = await client.post(
        "/api/settings", json={"changes": {"assistant": {"home_location": "Berlin"}}}
    )
    assert second.status_code == 200

    stored = yaml.safe_load(local_config(app).read_text(encoding="utf-8"))
    assert stored == {
        "assistant": {"user_name": "Nik", "home_location": "Berlin"},
        "voice": {"enabled": False},
    }
    assert app.loaded.path.read_text(encoding="utf-8") == original  # Hauptdatei unverändert
    reloaded = load_config(app.loaded.path).config
    assert reloaded.assistant.user_name == "Nik"
    assert reloaded.voice.enabled is False


@pytest.mark.parametrize(
    "changes",
    [
        {"budget": {"daily_limit_usd": -1}},
        {"unbekannt": {"x": 1}},
        {"voice": {"tts_engine": "lautsprecher"}},
        {"web": {"port": 80}},
    ],
)
async def test_settings_update_rejects_invalid(
    app: JarvisApp, client: httpx.AsyncClient, changes: dict[str, Any]
) -> None:
    response = await client.post("/api/settings", json={"changes": changes})
    assert response.status_code == 400
    assert response.json()["detail"].startswith("Ungültige Einstellung")
    assert not local_config(app).exists()


async def test_secret_set_allowed_name(
    client: httpx.AsyncClient, keyring_store: dict[tuple[str, str], str]
) -> None:
    secret = "AIza-test-geheim-1234567890"
    response = await client.post(
        "/api/secrets", json={"name": " gemini_api_key ", "value": f"  {secret} "}
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "source": "keyring"}
    assert secret not in response.text
    assert keyring_store == {("JARVIS", "GEMINI_API_KEY"): secret}

    settings = await client.get("/api/settings")
    assert settings.json()["secrets"]["GEMINI_API_KEY"] == "keyring"
    assert secret not in settings.text


@pytest.mark.parametrize("name", ["AWS_SECRET_ACCESS_KEY", "JARVIS_API_TOKEN", ""])
async def test_secret_unknown_name_rejected(
    client: httpx.AsyncClient, keyring_store: dict[tuple[str, str], str], name: str
) -> None:
    response = await client.post("/api/secrets", json={"name": name, "value": "geheim-123"})
    assert response.status_code == 400
    assert response.json()["detail"] == "Unbekannter Schlüsselname"
    assert "geheim-123" not in response.text
    assert keyring_store == {}


async def test_secret_without_keyring_fails_cleanly(client: httpx.AsyncClient) -> None:
    """Die Testkonfiguration hat ``secrets.use_keyring: false``."""
    response = await client.post(
        "/api/secrets", json={"name": "GEMINI_API_KEY", "value": "AIza-geheim"}
    )
    assert response.status_code == 400
    assert "keyring" in response.json()["detail"]
    assert "AIza-geheim" not in response.text


# ---------------------------------------------------------------------- Projekte & Selbstverbesserung


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/projects", {"goal": "Umzug planen"}),
        ("GET", "/api/projects/abc", None),
        ("POST", "/api/projects/abc/pause", None),
        ("POST", "/api/project-questions/1", {"answer": "ja"}),
    ],
)
async def test_projects_without_project_mode(
    client: httpx.AsyncClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    assert (await client.get("/api/projects")).json() == []
    response = await client.request(method, path, json=body)
    assert response.status_code == 503
    assert response.json() == {"detail": "Projektmodus nicht aktiv"}


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/improvements", None),
        ("POST", "/api/improvements", {"request": "Druckfunktion"}),
        ("GET", "/api/improvements/1", None),
        ("GET", "/api/improvements/1/diff", None),
        ("POST", "/api/improvements/1/approve", None),
        ("POST", "/api/improvements/1/reject", None),
        ("POST", "/api/improvements/1/cancel", None),
        ("POST", "/api/improvements/1/retry", None),
        ("POST", "/api/suggestions/1/accept", None),
        ("POST", "/api/suggestions/1/dismiss", None),
        ("POST", "/api/improvements-self-check", None),
    ],
)
async def test_improvements_without_self_improve(
    client: httpx.AsyncClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    response = await client.request(method, path, json=body)
    assert response.status_code == 503
    assert response.json() == {"detail": "Selbstverbesserung nicht aktiv"}


async def test_improvements_with_manager(app: JarvisApp, client: httpx.AsyncClient) -> None:
    """Mit (nicht gestartetem) Manager: Aufträge anlegen, ansehen, abbrechen, wiederholen."""
    app.improvements = ImprovementManager(app)
    assert (await client.post("/api/improvements", json={"request": "  "})).status_code == 400
    created = (await client.post("/api/improvements", json={"request": "Druckfunktion"})).json()
    assert created["status"] == "queued"
    assert created["source"] == "web"

    data = (await client.get("/api/improvements")).json()
    assert [j["id"] for j in data["jobs"]] == [created["id"]]
    assert data["suggestions"] == []
    assert data["auto_merge"] == "never"
    assert "unavailable" in data

    job = (await client.get(f"/api/improvements/{created['id']}")).json()
    assert job["request"] == "Druckfunktion"
    assert job["log"] == []
    assert (await client.get("/api/improvements/999")).status_code == 404
    diff = await client.get(f"/api/improvements/{created['id']}/diff")
    assert diff.text == ""
    assert diff.headers["content-type"].startswith("text/plain")

    cancel = await client.post(f"/api/improvements/{created['id']}/cancel")
    assert cancel.json() == {"ok": True}
    retry = (await client.post(f"/api/improvements/{created['id']}/retry")).json()
    assert retry["id"] != created["id"]
    assert retry["request"] == "Druckfunktion"
    assert retry["source"] == "retry"
    assert (await client.post("/api/improvements/999/retry")).status_code == 404
    assert (await client.post("/api/suggestions/999/accept")).status_code == 404

    overview = (await client.get("/api/overview")).json()
    assert [j["id"] for j in overview["improvements"]] == [retry["id"], created["id"]]


# ---------------------------------------------------------------------- Sprache & System


async def test_voice_endpoints_without_voice(client: httpx.AsyncClient) -> None:
    transcribe = await client.post(
        "/api/voice/transcribe", files={"file": ("aufnahme.webm", b"\x00\x01", "audio/webm")}
    )
    assert transcribe.status_code == 503
    assert (await client.post("/api/voice/speak", json={"text": "Hallo"})).status_code == 503
    assert (await client.post("/api/voice/listening", json={"enabled": True})).status_code == 503
    assert (await client.post("/api/voice/ptt")).status_code == 503


async def test_restart_and_shutdown_without_service(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/system/restart")).status_code == 503
    assert (await client.post("/api/system/shutdown")).status_code == 503


async def test_restart_and_shutdown_are_delayed(app: JarvisApp, chat: ChatService) -> None:
    calls: list[str] = []
    web = create_web_app(
        app,
        chat,
        token=TOKEN,
        port=PORT,
        restart=lambda: calls.append("restart"),
        shutdown=lambda: calls.append("shutdown"),
    )
    async with make_client(web, AUTH) as http:
        assert (await http.post("/api/system/restart")).json() == {"ok": True}
        assert (await http.post("/api/system/shutdown")).json() == {"ok": True}
        assert calls == []  # erst nach der Antwort, damit der Browser sie noch bekommt
        await asyncio.sleep(0.7)
    assert sorted(calls) == ["restart", "shutdown"]


# ---------------------------------------------------------------------- WebSocket


@pytest.mark.parametrize("origin", [None, BASE, f"http://localhost:{PORT}"])
def test_ws_accepts_token_and_local_origin(sync_client: TestClient, origin: str | None) -> None:
    headers = {"origin": origin} if origin else {}
    with sync_client.websocket_connect(WS_URL, headers=headers) as ws:
        assert ws.receive_json() == {"type": "hello", "data": {"version": __version__}}


@pytest.mark.parametrize(
    ("url", "headers"),
    [
        (WS_URL, {"origin": "http://evil.example"}),
        (WS_URL, {"origin": f"http://127.0.0.1:{PORT + 1}"}),
        (WS_URL, {"origin": f"https://127.0.0.1:{PORT}"}),
        (WS_URL, {"origin": "null"}),
        (f"ws://127.0.0.1:{PORT}/ws", {}),
        (f"ws://127.0.0.1:{PORT}/ws?t=falsch", {}),
        (f"ws://127.0.0.1:{PORT}/ws?t=%C3%A4", {}),
        (f"ws://testserver/ws?t={TOKEN}", {}),
        (f"ws://evil.example:{PORT}/ws?t={TOKEN}", {}),
    ],
)
def test_ws_rejects_foreign_origin_host_or_token(
    sync_client: TestClient, url: str, headers: dict[str, str]
) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as info,
        sync_client.websocket_connect(url, headers=headers),
    ):
        pass  # pragma: no cover - Verbindung wird vorher geschlossen
    assert info.value.code == 4403


def test_ws_streams_chat_answer(sync_client: TestClient) -> None:
    """Das Dashboard schickt per POST und bekommt die Antwort live per WebSocket."""
    with sync_client.websocket_connect(WS_URL, headers={"origin": BASE}) as ws:
        assert ws.receive_json()["type"] == "hello"
        response = sync_client.post("/api/chat", json={"text": "Hallo"}, headers=AUTH)
        conversation = response.json()["conversation_id"]
        events: list[dict[str, Any]] = []
        for _ in range(50):
            event = ws.receive_json()
            if event["type"].startswith("chat."):
                events.append(event)
            if event["type"] == "chat.final":
                break
    assert [e["type"] for e in events] == ["chat.started", "chat.delta", "chat.delta", "chat.final"]
    assert {e["session_id"] for e in events} == {conversation}
    assert "".join(e["data"]["text"] for e in events[1:3]) == "Hallo Welt"
    assert events[-1]["data"]["text"] == "Hallo Welt"
    assert events[-1]["data"]["model"] == "fake-big"


def test_ws_receives_killswitch_and_confirmation_events(sync_client: TestClient) -> None:
    with sync_client.websocket_connect(WS_URL) as ws:
        assert ws.receive_json()["type"] == "hello"
        sync_client.post("/api/killswitch", json={"engage": True}, headers=AUTH)
        event = ws.receive_json()
        assert event["type"] == "killswitch.engaged"
        assert event["data"] == {"reason": "Dashboard", "by": "web"}
        sync_client.post("/api/killswitch", json={"engage": False}, headers=AUTH)
        assert ws.receive_json()["type"] == "killswitch.released"


# ---------------------------------------------------------------------- Google-Einrichtung


DESKTOP_CLIENT = (
    b'{"installed": {"client_id": "x.apps.googleusercontent.com", "client_secret": "s"}}'
)


async def test_google_client_upload_validates_type(
    client: httpx.AsyncClient, app: JarvisApp
) -> None:
    status = (await client.get("/api/google")).json()
    assert status["configured"] is False and status["accounts"] == []

    web_client = b'{"web": {"client_id": "x", "client_secret": "s"}}'
    response = await client.post("/api/google/client", files={"file": ("c.json", web_client)})
    assert response.status_code == 400 and "Desktop-App" in response.json()["detail"]
    response = await client.post("/api/google/client", files={"file": ("c.json", b"kaputt")})
    assert response.status_code == 400
    assert not app.google.client_file.exists()

    response = await client.post("/api/google/client", files={"file": ("c.json", DESKTOP_CLIENT)})
    assert response.status_code == 200
    assert app.google.configured()
    assert (await client.get("/api/google")).json()["configured"] is True


async def test_google_add_account_runs_login_in_background(
    client: httpx.AsyncClient, app: JarvisApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (await client.post("/api/google/accounts")).status_code == 409  # ohne OAuth-Datei
    app.google.save_client(DESKTOP_CLIENT)
    monkeypatch.setattr(app.google, "add_account", lambda: "nik@gmail.com")
    with app.events.subscribe(types={"google.account_added", "google.error"}) as sub:
        response = await client.post("/api/google/accounts")
        assert response.status_code == 200 and response.json() == {"started": True}
        event = await sub.get(limit_s=5)
    assert event is not None and event.type == "google.account_added"
    assert event.data["email"] == "nik@gmail.com"
    assert (await client.get("/api/google")).json()["login_running"] is False


async def test_google_login_error_is_reported(
    client: httpx.AsyncClient, app: JarvisApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    app.google.save_client(DESKTOP_CLIENT)

    def fail() -> str:
        raise TimeoutError("Keine Anmeldung im Browser")

    monkeypatch.setattr(app.google, "add_account", fail)
    with app.events.subscribe(types={"google.error"}) as sub:
        assert (await client.post("/api/google/accounts")).status_code == 200
        event = await sub.get(limit_s=5)
    assert event is not None and "Keine Anmeldung" in event.data["error"]


async def test_google_remove_unknown_account(client: httpx.AsyncClient) -> None:
    response = await client.delete("/api/google/accounts/nie@gmail.com")
    assert response.status_code == 404
