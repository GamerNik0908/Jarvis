"""Gedächtnis, Gesprächsverlauf, Ereignisbus, Zeitplaner sowie JSON- und Zeit-Helfer."""

from __future__ import annotations

import asyncio
import itertools
import os
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import jarvis.core.db as db_module
from jarvis.core import events as events_module
from jarvis.core.app import JarvisApp
from jarvis.core.db import Database
from jarvis.core.events import Event, EventBus, Subscription
from jarvis.core.jsonutil import extract_json
from jarvis.core.scheduler import Scheduler, is_due, parse_hhmm
from jarvis.core.timeutil import human, now, now_iso, parse_iso
from jarvis.memory.conversations import ConversationStore
from jarvis.memory.store import Embedder, Memory, MemoryStore, cosine

from .conftest import FrozenClock

# ---------------------------------------------------------------- Gedächtnis

HUND = "Bello ist der Hund"
KATZE = "Minka ist die Katze"
KAFFEE = "Kaffee bitte schwarz"
TIER_FRAGE = "Welche Haustiere gibt es?"  # kein gemeinsames Wort mit den Einträgen

VECTORS: dict[str, list[float]] = {
    HUND: [1.0, 0.0, 0.0, 0.0],
    KATZE: [0.6, 0.8, 0.0, 0.0],
    KAFFEE: [0.0, 0.0, 1.0, 0.0],
    TIER_FRAGE: [0.8, 0.6, 0.0, 0.0],  # cos: Katze 0.96, Hund 0.8, Kaffee 0
    "Kaffee": [0.0, 0.0, 1.0, 0.0],
    "schwarz": [1.0, 0.0, 0.0, 0.0],  # zeigt semantisch auf den Hund
}


class FakeEmbedder:
    """Feste Vektoren pro Text; unbekannte Texte zeigen in eine eigene Richtung."""

    def __init__(self, *, name: str = "fake:embed") -> None:
        self.calls: list[list[str]] = []
        self.error: Exception | None = None
        self.embedder = Embedder(name=name, embed=self.embed)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.error is not None:
            raise self.error
        return [VECTORS.get(text, [0.0, 0.0, 0.0, 1.0]) for text in texts]


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def db_without_fts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Database]:
    """Datenbank, deren optionale FTS5-Migration fehlschlägt (wie bei SQLite ohne FTS5)."""
    monkeypatch.setattr(
        db_module,
        "OPTIONAL_MIGRATIONS",
        ((3, "CREATE VIRTUAL TABLE memories_fts USING gibt_es_nicht(text);"),),
    )
    database = Database(tmp_path / "ohne_fts.db")
    assert database.migrate() == 3  # übersprungen, aber als erledigt vermerkt
    yield database
    database.close()


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ([1.0, 0.0], [1.0, 0.0], 1.0),
        ([3.0, 4.0], [6.0, 8.0], 1.0),  # Länge egal, nur die Richtung zählt
        ([1.0, 0.0], [0.0, 1.0], 0.0),
        ([1.0, 0.0], [-1.0, 0.0], -1.0),
        ([0.0, 0.0], [1.0, 1.0], 0.0),  # Nullvektor → keine Division durch null
        ([0.0, 0.0], [0.0, 0.0], 0.0),
        ([], [], 0.0),
        ([1.0, 2.0], [1.0, 2.0, 3.0], 0.0),  # unterschiedliche Dimensionen
    ],
)
def test_cosine(a: list[float], b: list[float], expected: float) -> None:
    assert cosine(a, b) == pytest.approx(expected)


async def test_remember_stores_embedding_and_metadata(
    db: Database, fake_embedder: FakeEmbedder
) -> None:
    store = MemoryStore(db, fake_embedder.embedder)
    memory_id = await store.remember(f"  {HUND}\n", category="haustier", source="test")
    assert fake_embedder.calls == [[HUND]]  # bereits ohne Leerraum
    row = db.query_one("SELECT * FROM memories WHERE id = ?", (memory_id,))
    assert row is not None
    assert row["text"] == HUND
    assert (row["category"], row["source"]) == ("haustier", "test")
    assert row["embedding_model"] == "fake:embed"
    assert len(row["embedding"]) == 4 * 4  # vier float32-Werte
    assert store.count() == 1


async def test_remember_rejects_empty_text(db: Database) -> None:
    store = MemoryStore(db)
    for text in ("", "   \n"):
        with pytest.raises(ValueError):
            await store.remember(text)
    assert store.count() == 0


async def test_remember_deduplicates_case_insensitively(
    db: Database, fake_embedder: FakeEmbedder
) -> None:
    store = MemoryStore(db, fake_embedder.embedder)
    first = await store.remember(HUND)
    assert await store.remember(HUND.upper()) == first
    assert await store.remember(f" {HUND} ") == first
    assert store.count() == 1
    assert len(fake_embedder.calls) == 1  # Duplikate werden nicht neu eingebettet


async def test_remember_deduplicates_umlauts(db: Database) -> None:
    # SQLite-lower() kennt nur ASCII – "Ä" und "ä" müssen trotzdem als gleich gelten.
    store = MemoryStore(db)
    first = await store.remember("Äpfel mag ich nicht")
    assert await store.remember("äpfel mag ich nicht") == first
    assert await store.remember("ÄPFEL MAG ICH NICHT") == first
    assert store.count() == 1


async def test_all_lists_newest_first_and_to_dict(db: Database) -> None:
    store = MemoryStore(db)
    first = await store.remember(HUND)
    second = await store.remember(KATZE, category="haustier")
    memories = store.all()
    assert [m.id for m in memories] == [second, first]
    assert [m.id for m in store.all(limit=1)] == [second]
    data = memories[0].to_dict()
    assert data == {
        "id": second,
        "text": KATZE,
        "category": "haustier",
        "created": memories[0].created,
    }
    assert "score" not in data
    assert isinstance(memories[0], Memory)


async def test_forget_removes_entry_and_fulltext_index(db: Database) -> None:
    store = MemoryStore(db)
    memory_id = await store.remember(HUND)
    other = await store.remember(KATZE)
    assert [m.id for m in await store.recall("Hund")] == [memory_id]
    assert store.forget(memory_id) is True
    assert store.forget(memory_id) is False  # schon weg
    assert store.forget(9999) is False
    assert await store.recall("Hund") == []  # FTS-Trigger hat den Eintrag entfernt
    assert [m.id for m in store.all()] == [other]


async def test_recall_ranks_by_cosine_similarity(db: Database, fake_embedder: FakeEmbedder) -> None:
    store = MemoryStore(db, fake_embedder.embedder)
    hund = await store.remember(HUND)
    katze = await store.remember(KATZE)
    await store.remember(KAFFEE)
    results = await store.recall(TIER_FRAGE)
    # Kaffee (cos 0) liegt unter der Schwelle von 0.35 und fehlt.
    assert [m.id for m in results] == [katze, hund]
    assert results[0].score == pytest.approx(0.96, abs=1e-6)
    assert results[1].score == pytest.approx(0.8, abs=1e-6)
    assert [m.id for m in await store.recall(TIER_FRAGE, limit=1)] == [katze]


async def test_recall_mixes_fulltext_and_vector_scores(
    db: Database, fake_embedder: FakeEmbedder
) -> None:
    store = MemoryStore(db, fake_embedder.embedder)
    hund = await store.remember(HUND)
    katze = await store.remember(KATZE)
    kaffee = await store.remember(KAFFEE)
    # "schwarz": Volltexttreffer Kaffee (0.5), semantisch Hund (1.0) und Katze (0.6).
    results = await store.recall("schwarz")
    assert [(m.id, round(m.score, 3)) for m in results] == [
        (hund, 1.0),
        (katze, 0.6),
        (kaffee, 0.5),
    ]
    # Treffer in beiden Suchen: der bessere Wert zählt, kein Doppelter.
    both = await store.recall("Kaffee")
    assert [m.id for m in both] == [kaffee]
    assert both[0].score == pytest.approx(1.0)


async def test_failing_embedder_falls_back_to_fulltext(
    db: Database, fake_embedder: FakeEmbedder
) -> None:
    assert db.has_table("memories_fts")  # optionale Migration 3 ist gelaufen
    fake_embedder.error = RuntimeError("Ollama läuft nicht")
    store = MemoryStore(db, fake_embedder.embedder)
    memory_id = await store.remember(HUND)
    row = db.query_one("SELECT embedding, embedding_model FROM memories WHERE id = ?", (memory_id,))
    assert row is not None
    assert row["embedding"] is None and row["embedding_model"] is None
    results = await store.recall("Wie heißt der Hund?")
    assert [(m.id, m.text, m.score) for m in results] == [(memory_id, HUND, 0.5)]
    assert len(fake_embedder.calls) == 2  # beim Merken und beim Suchen versucht


async def test_embedder_failing_only_at_recall_uses_fulltext(
    db: Database, fake_embedder: FakeEmbedder
) -> None:
    store = MemoryStore(db, fake_embedder.embedder)
    await store.remember(HUND)
    katze = await store.remember(KATZE)
    fake_embedder.error = TimeoutError()
    assert await store.recall(TIER_FRAGE) == []  # semantisch wäre etwas gekommen
    assert [m.id for m in await store.recall("Katze")] == [katze]


async def test_recall_ignores_vectors_of_other_embedding_model(
    db: Database, fake_embedder: FakeEmbedder
) -> None:
    store = MemoryStore(db, fake_embedder.embedder)
    await store.remember(HUND)
    await store.remember(KATZE)
    store.embedder = Embedder(name="anderes:modell", embed=fake_embedder.embed)
    assert await store.recall(TIER_FRAGE) == []


async def test_recall_without_embedder_uses_fulltext(db: Database) -> None:
    store = MemoryStore(db)
    hund = await store.remember(HUND)
    katze = await store.remember(KATZE)
    await store.remember(KAFFEE)
    results = await store.recall("Wo sind Hund und Katze?")
    assert {m.id for m in results} == {hund, katze}
    assert all(m.score == 0.5 for m in results)
    assert await store.recall("Hund", limit=1) != []
    assert await store.recall(TIER_FRAGE) == []
    assert await store.recall("?! -- ++") == []  # keine Wörter → keine FTS-Abfrage
    assert await store.recall('"Hund" OR (NEAR') != []  # Sonderzeichen stören nicht


async def test_recall_without_fts_uses_like(db_without_fts: Database) -> None:
    store = MemoryStore(db_without_fts)
    assert not db_without_fts.has_table("memories_fts")
    hund = await store.remember(HUND)
    await store.remember(KAFFEE)
    results = await store.recall("HUND")
    assert [(m.id, m.score) for m in results] == [(hund, 0.4)]
    assert [m.id for m in await store.recall("bell")] == [hund]  # Teilwort genügt
    assert await store.recall("ab xy") == []  # Wörter unter drei Zeichen zählen nicht
    assert store.forget(hund) is True
    assert await store.recall("Hund") == []


# ---------------------------------------------------------------- Gespräche


@pytest.fixture
def conversations(db: Database, monkeypatch: pytest.MonkeyPatch) -> ConversationStore:
    """Jeder Zeitstempel eine Sekunde später – sonst wäre die Reihenfolge in ``list`` Zufall."""
    start = datetime(2026, 10, 12, 14, 30, tzinfo=UTC)
    ticks = itertools.count()
    monkeypatch.setattr(
        "jarvis.memory.conversations.now_iso",
        lambda: (start + timedelta(seconds=next(ticks))).isoformat(timespec="seconds"),
    )
    return ConversationStore(db)


def test_create_and_exists(conversations: ConversationStore) -> None:
    cid = conversations.create(channel="voice", title="Start")
    assert len(cid) == 12 and int(cid, 16) >= 0
    assert conversations.exists(cid)
    assert not conversations.exists("gibt-es-nicht")
    (entry,) = conversations.list()
    assert entry["id"] == cid
    assert (entry["title"], entry["channel"]) == ("Start", "voice")
    assert (entry["sensitive"], entry["archived"]) == (0, 0)
    assert conversations.create() != cid


def test_first_user_message_becomes_title(conversations: ConversationStore) -> None:
    cid = conversations.create()
    conversations.add(cid, "assistant", "Guten Tag, ich bin JARVIS.")
    assert conversations.list()[0]["title"] == ""  # Antworten setzen keinen Titel
    question = "Wie wird das Wetter morgen in Berlin? Bitte mit Regenwahrscheinlichkeit und Wind"
    conversations.add(cid, "user", f"  {question}\nZweite Zeile")
    assert conversations.list()[0]["title"] == question[:60]
    conversations.add(cid, "user", "Und übermorgen?")
    assert conversations.list()[0]["title"] == question[:60]


def test_blank_first_message_and_explicit_title(conversations: ConversationStore) -> None:
    blank = conversations.create()
    conversations.add(blank, "user", "   ")
    titled = conversations.create(title="Eigener Titel")
    conversations.add(titled, "user", "Hallo")
    titles = {c["id"]: c["title"] for c in conversations.list()}
    assert titles == {blank: "Gespräch", titled: "Eigener Titel"}
    conversations.rename(titled, "Neu")
    assert {c["id"]: c["title"] for c in conversations.list()}[titled] == "Neu"


def test_messages_order_meta_and_limit(conversations: ConversationStore) -> None:
    cid = conversations.create()
    other = conversations.create()
    ids = [
        conversations.add(cid, "user", "eins", {"quelle": "Sprache", "ä": [1, 2]}),
        conversations.add(cid, "assistant", "zwei"),
        conversations.add(other, "user", "fremd"),
        conversations.add(cid, "user", "drei"),
    ]
    assert ids == sorted(ids)
    messages = conversations.messages(cid)
    assert [m.content for m in messages] == ["eins", "zwei", "drei"]
    assert messages[0].meta == {"quelle": "Sprache", "ä": [1, 2]}
    assert messages[1].meta == {}
    assert messages[1].to_dict() == {
        "id": ids[1],
        "role": "assistant",
        "content": "zwei",
        "created": messages[1].created,
        "meta": {},
    }
    # limit liefert die letzten Nachrichten, aber in zeitlicher Reihenfolge
    assert [m.content for m in conversations.messages(cid, limit=2)] == ["zwei", "drei"]
    assert conversations.messages("gibt-es-nicht") == []


def test_list_orders_by_activity_and_filters(
    conversations: ConversationStore, db: Database
) -> None:
    older = conversations.create(channel="chat")
    voice = conversations.create(channel="voice")
    hidden = conversations.create(channel="chat")
    db.execute("UPDATE conversations SET archived = 1 WHERE id = ?", (hidden,))
    assert [c["id"] for c in conversations.list()] == [voice, older]
    conversations.add(older, "user", "neu")  # wird wieder aktuell
    assert [c["id"] for c in conversations.list()] == [older, voice]
    assert [c["id"] for c in conversations.list(limit=1)] == [older]
    assert [c["id"] for c in conversations.list(channel="voice")] == [voice]
    assert conversations.list(channel="projekt") == []


def test_ensure_reuses_or_creates(conversations: ConversationStore) -> None:
    cid = conversations.create()
    assert conversations.ensure(cid) == cid
    fresh = conversations.ensure("gibt-es-nicht", channel="voice")
    assert fresh != cid and conversations.exists(fresh)
    assert conversations.list(channel="voice")[0]["id"] == fresh
    assert conversations.ensure(None) not in (cid, fresh)


def test_sensitive_flag(conversations: ConversationStore) -> None:
    cid = conversations.create()
    assert conversations.is_sensitive(cid) is False
    conversations.set_sensitive(cid)
    assert conversations.is_sensitive(cid) is True
    conversations.set_sensitive(cid, False)
    assert conversations.is_sensitive(cid) is False
    assert conversations.is_sensitive("gibt-es-nicht") is False


def test_delete_cascades_messages(conversations: ConversationStore, db: Database) -> None:
    cid = conversations.create()
    keep = conversations.create()
    conversations.add(cid, "user", "weg damit")
    conversations.add(keep, "user", "bleibt")
    conversations.delete(cid)
    assert not conversations.exists(cid)
    row = db.query_one("SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (cid,))
    assert row is not None and row[0] == 0
    assert [m.content for m in conversations.messages(keep)] == ["bleibt"]
    conversations.delete(cid)  # erneut löschen ist harmlos


def test_add_to_unknown_conversation_fails(conversations: ConversationStore) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conversations.add("gibt-es-nicht", "user", "Hallo")


# ---------------------------------------------------------------- Ereignisbus


def _drain(sub: Subscription) -> list[Event]:
    events: list[Event] = []
    while not sub.queue.empty():
        events.append(sub.queue.get_nowait())
    return events


def test_publish_copies_data_and_records_history() -> None:
    bus = EventBus()
    data: dict[str, Any] = {"text": "Hallo"}
    first = bus.publish("chat.delta", data, session_id="s1")  # ohne Abonnenten
    data["text"] = "geändert"
    second = bus.publish("chat.done")
    assert first.data == {"text": "Hallo"}
    assert second.data == {}
    assert second.id > first.id
    assert first.to_dict() == {
        "id": first.id,
        "type": "chat.delta",
        "data": {"text": "Hallo"},
        "session_id": "s1",
        "ts": first.ts,
    }
    assert bus.recent() == [first, second]


async def test_type_and_prefix_filters() -> None:
    bus = EventBus()
    everything = bus.subscribe()
    by_type = bus.subscribe(types=["tool.started"])
    by_prefix = bus.subscribe(prefixes=["chat."])
    combined = bus.subscribe(types=["notification"], prefixes=["tool."])
    for kind in ("chat.delta", "tool.started", "tool.finished", "notification", "image.new"):
        bus.publish(kind)
    assert len(_drain(everything)) == 5
    assert [e.type for e in _drain(by_type)] == ["tool.started"]
    assert [e.type for e in _drain(by_prefix)] == ["chat.delta"]
    assert [e.type for e in _drain(combined)] == ["tool.started", "tool.finished", "notification"]


async def test_session_filter() -> None:
    bus = EventBus()
    session_a = bus.subscribe(session_id="a")
    a_chat_only = bus.subscribe(session_id="a", types=["chat.delta"])
    everyone = bus.subscribe()
    bus.publish("chat.delta", session_id="a")
    bus.publish("chat.delta", session_id="b")
    bus.publish("notification")  # globale Ereignisse erreichen jede Sitzung
    assert [e.session_id for e in _drain(session_a)] == ["a", None]
    assert [(e.type, e.session_id) for e in _drain(a_chat_only)] == [("chat.delta", "a")]
    assert [e.session_id for e in _drain(everyone)] == ["a", "b", None]


async def test_get_waits_and_times_out() -> None:
    bus = EventBus()
    sub = bus.subscribe()
    started = time.monotonic()
    assert await sub.get(limit_s=0.01) is None
    assert time.monotonic() - started < 1
    event = bus.publish("tool.started")
    assert await sub.get(limit_s=0.5) is event
    later = asyncio.get_running_loop().call_later(0.01, bus.publish, "tool.finished")
    received = await sub.get()  # ohne Limit: wartet, bis etwas kommt
    assert received is not None and received.type == "tool.finished"
    later.cancel()


async def test_async_iteration() -> None:
    bus = EventBus()
    sub = bus.subscribe(prefixes=["step."])
    bus.publish("step.0")

    async def collect() -> list[str]:
        seen: list[str] = []
        async for event in sub:
            seen.append(event.type)
            if len(seen) == 3:
                break
        return seen

    consumer = asyncio.create_task(collect())
    await asyncio.sleep(0)
    bus.publish("anderes")  # passt nicht zum Filter
    bus.publish("step.1")
    bus.publish("step.2")
    assert await asyncio.wait_for(consumer, 1) == ["step.0", "step.1", "step.2"]


async def test_context_manager_closes_subscription() -> None:
    bus = EventBus()
    with bus.subscribe() as sub:
        bus.publish("vorher")
        received = await sub.get(limit_s=0.1)
        assert received is not None and received.type == "vorher"
    assert sub not in bus._subscribers
    bus.publish("nachher")
    assert sub.queue.empty()
    sub.close()  # doppelt schließen ist harmlos


async def test_publish_from_thread_after_bind_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = EventBus()
    bus.bind_loop(asyncio.get_running_loop())
    sub = bus.subscribe()
    put_threads: list[int] = []
    original = events_module._put

    def spy(queue: asyncio.Queue[Event], event: Event) -> None:
        put_threads.append(threading.get_ident())
        original(queue, event)

    monkeypatch.setattr(events_module, "_put", spy)
    waiter = asyncio.create_task(sub.get(limit_s=2))
    await asyncio.sleep(0)  # Abholer wartet bereits
    published = await asyncio.to_thread(bus.publish, "voice.state", {"state": "hört zu"})
    assert await waiter is published
    # call_soon_threadsafe: eingereiht wird im Thread der Ereignisschleife
    assert put_threads == [threading.get_ident()]


async def test_publish_on_bound_loop_is_immediate() -> None:
    bus = EventBus()
    bus.bind_loop(asyncio.get_running_loop())
    sub = bus.subscribe()
    event = bus.publish("chat.delta")
    assert sub.queue.get_nowait() is event  # kein Umweg über call_soon_threadsafe


def test_publish_after_bound_loop_closed_does_not_raise() -> None:
    # Beim Herunterfahren dürfen Tray/Sprache noch senden, obwohl die Schleife zu ist.
    loop = asyncio.new_event_loop()
    loop.close()
    bus = EventBus()
    bus.bind_loop(loop)
    sub = bus.subscribe()
    event = bus.publish("tray.click")
    assert bus.recent() == [event]
    assert sub.queue.empty()


def test_recent_limit_prefix_and_history_size() -> None:
    bus = EventBus(history=4)
    for kind in ("chat.delta", "tool.started", "chat.delta", "chat.done", "image.new"):
        bus.publish(kind)
    assert [e.type for e in bus.recent()] == [
        "tool.started",
        "chat.delta",
        "chat.done",
        "image.new",
    ]  # das älteste ist aus dem Verlauf gefallen
    assert [e.type for e in bus.recent(prefix="chat.")] == ["chat.delta", "chat.done"]
    assert [e.type for e in bus.recent(1, prefix="chat.")] == ["chat.done"]
    assert [e.type for e in bus.recent(2)] == ["chat.done", "image.new"]
    assert bus.recent(0) == []
    assert bus.recent(prefix="gibt.es.nicht") == []


async def test_full_queue_drops_events_without_raising() -> None:
    bus = EventBus()
    sub = bus.subscribe()
    for index in range(1005):
        bus.publish("chat.delta", {"i": index})
    assert sub.queue.qsize() == 1000
    assert sub.queue.get_nowait().data == {"i": 0}  # die ältesten bleiben erhalten
    assert len(bus.recent(2000)) == 300  # Verlauf ist unabhängig davon begrenzt


async def test_full_queue_via_thread_does_not_break_loop() -> None:
    loop = asyncio.get_running_loop()
    errors: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    try:
        bus = EventBus()
        bus.bind_loop(loop)
        sub = bus.subscribe()

        def flood() -> None:
            for index in range(1010):
                bus.publish("chat.delta", {"i": index})

        await asyncio.to_thread(flood)
        await asyncio.sleep(0.01)  # eingereihte Rückrufe abarbeiten
        assert sub.queue.qsize() == 1000
        assert errors == []
    finally:
        loop.set_exception_handler(None)


# ---------------------------------------------------------------- Zeitplaner

MONDAY = datetime(2026, 10, 12, 14, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("08:05", (8, 5)), ("7", (7, 0)), ("7:", (7, 0)), ("25:99", (23, 59)), ("-1:30", (0, 30))],
)
def test_parse_hhmm(value: str, expected: tuple[int, int]) -> None:
    assert parse_hhmm(value) == expected


@pytest.mark.parametrize(
    ("moment", "at", "last", "expected"),
    [
        (MONDAY, "14:00", None, True),  # Zeit vorbei, noch nie gelaufen
        (MONDAY, "14:30", None, True),  # genau zur Minute
        (MONDAY, "14:31", None, False),  # eine Minute zu früh
        (MONDAY, "20:00", "2026-10-11", False),  # gestern gelaufen, heute noch zu früh
        (MONDAY, "14:00", "2026-10-12", False),  # heute schon gelaufen
        (MONDAY, "14:00", "2026-10-11", True),  # gestern gelaufen
        (MONDAY, "8", "2026-10-05", True),  # Stunde ohne Minuten
    ],
)
def test_is_due_daily(moment: datetime, at: str, last: str | None, expected: bool) -> None:
    assert is_due(moment, at, last) is expected


@pytest.mark.parametrize(
    ("moment", "last", "expected"),
    [
        (MONDAY, None, True),
        (MONDAY.replace(hour=2), None, False),  # vor 03:00
        (MONDAY, "2026-10-12", False),  # heute schon
        (MONDAY + timedelta(days=2), "2026-10-12", False),  # Mittwoch derselben Woche
        (datetime(2026, 10, 18, 23, 59, tzinfo=UTC), "2026-10-12", False),  # erst 6 Tage
        (datetime(2026, 10, 19, 2, 59, tzinfo=UTC), "2026-10-12", False),  # Tag 7, zu früh
        (datetime(2026, 10, 19, 3, 0, tzinfo=UTC), "2026-10-12", True),
        (datetime(2026, 11, 2, 3, 0, tzinfo=UTC), "2026-10-12", True),  # länger verpasst
        (MONDAY, "kaputt", True),  # unlesbarer Zustand → lieber ausführen
    ],
)
def test_is_due_weekly(moment: datetime, last: str | None, expected: bool) -> None:
    assert is_due(moment, "03:00", last, weekly=True) is expected


@pytest.fixture
async def app(config_file: Path) -> AsyncIterator[JarvisApp]:
    application = JarvisApp.bootstrap(config_file, role="test", detect=False)
    yield application
    await application.aclose()


async def test_fire_reminders(app: JarvisApp, clock: FrozenClock) -> None:
    moment = clock.now  # Mo 12.10.2026 14:30 Ortszeit
    once = app.reminders.create("Müll rausbringen", moment - timedelta(minutes=5))
    daily = app.reminders.create("Tabletten", moment - timedelta(hours=1), repeat="daily")
    later = app.reminders.create("Zahnarzt", moment + timedelta(hours=2))
    toasts: list[tuple[str, str]] = []
    app.notify_handlers.append(lambda title, message: toasts.append((title, message)))
    spoken: list[str] = []
    app.voice = SimpleNamespace(say_soon=spoken.append)  # type: ignore[assignment]

    scheduler = Scheduler(app)
    scheduler.fire_reminders(moment)

    due = app.events.recent(prefix="reminder.")
    assert [e.type for e in due] == ["reminder.due", "reminder.due"]
    assert [e.data for e in due] == [daily.to_dict(), once.to_dict()]  # nach Fälligkeit
    expected = [
        ("Erinnerung", f"Tabletten ({human(daily.due)})"),
        ("Erinnerung", f"Müll rausbringen ({human(once.due)})"),
    ]
    assert toasts == expected
    assert expected[1][1] == "Müll rausbringen (Mo 12.10. 14:25)"
    assert spoken == [message for _, message in expected]
    notes = app.events.recent(prefix="notification")
    assert [(e.data["title"], e.data["message"]) for e in notes] == expected

    remaining = {r.id: r for r in app.reminders.upcoming()}
    assert set(remaining) == {daily.id, later.id}  # einmalige Erinnerung ist erledigt
    assert remaining[daily.id].due == daily.due + timedelta(days=1)

    scheduler.fire_reminders(moment)  # nichts wird doppelt ausgelöst
    assert len(app.events.recent(prefix="reminder.")) == 2


@pytest.mark.parametrize(
    ("report", "titles"),
    [("Bericht: 2 Aufgaben erledigt", ["Tagesbericht"]), ("Es gibt keine aktiven Projekte.", [])],
)
async def test_tick_runs_daily_report_and_self_check_once(
    app: JarvisApp, report: str, titles: list[str]
) -> None:
    reports: list[datetime] = []
    checked = asyncio.Event()
    checks: list[str] = []

    class Projects:
        def daily_report(self, moment: datetime) -> str:
            reports.append(moment)
            return report

    class Improver:
        async def self_check(self) -> None:
            checks.append("ok")
            checked.set()

    app.projects = Projects()  # type: ignore[assignment]
    app.improvements = Improver()  # type: ignore[assignment]
    moment = datetime(2026, 10, 12, 21, 0).astimezone()  # nach 20:00 und nach 03:00
    scheduler = Scheduler(app)
    await scheduler.tick(moment)
    await asyncio.wait_for(checked.wait(), 1)  # Selbst-Check läuft als Hintergrund-Task
    assert reports == [moment]
    assert app.db.get_state("daily_report_day") == "2026-10-12"
    assert app.db.get_state("self_check_day") == "2026-10-12"
    assert [e.data["title"] for e in app.events.recent(prefix="notification")] == titles

    await scheduler.tick(moment + timedelta(minutes=30))
    await asyncio.sleep(0)
    assert len(reports) == 1 and checks == ["ok"]


# ---------------------------------------------------------------- JSON aus Modellantworten


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ("  [1, 2, 3]\n", [1, 2, 3]),
        ('```json\n{"plan": ["a", "b"]}\n```', {"plan": ["a", "b"]}),
        ('Hier der Plan:\n```\n{"ok": true}\n```\nViel Erfolg!', {"ok": True}),
        ('Klar! {"title": "Test", "done": false} – fertig.', {"title": "Test", "done": False}),
        ('Ergebnis: [{"t": "A"}, {"t": "B"}] Ende.', [{"t": "A"}, {"t": "B"}]),
        ('Siehe [Quelle] und [{"a": 1}]', [{"a": 1}]),  # ungültige Klammer davor
        ('Schritt [1] von 2: {"a": 1}', {"a": 1}),  # Liste davor gehört nicht dazu
        ('{"a": 1} und danach [{"b": 2}]', {"a": 1}),  # das erste Objekt gewinnt
        (
            'Antwort: {"items": [[1, 2], [3]], "meta": {"tags": []}} ok',
            {"items": [[1, 2], [3]], "meta": {"tags": []}},
        ),
        ("Liste: [[1, 2], [3, 4]] fertig", [[1, 2], [3, 4]]),
        ('Format {name}: {"a": 1}', {"a": 1}),  # ungültiges Objekt davor wird übersprungen
        ('```json\nkaputt\n```\nAber hier: {"a": 1}', {"a": 1}),  # kaputter Codeblock
        ('Text ```json\n{"a": 1}\n``` und {"b": 2}', {"a": 1}),  # Codeblock hat Vorrang
    ],
)
def test_extract_json(text: str, expected: Any) -> None:
    assert extract_json(text) == expected


@pytest.mark.parametrize(
    "text", ["", "Keine Ahnung.", "Die Antwort ist 42.", "{kaputt", "[1, 2", "```json\n```"]
)
def test_extract_json_without_structure_raises(text: str) -> None:
    with pytest.raises(ValueError, match="Keine gültige JSON-Struktur"):
        extract_json(text)


# ---------------------------------------------------------------- Zeit-Helfer


@pytest.fixture
def berlin() -> Iterator[None]:
    """Lokale Zeitzone Europe/Berlin (im Oktober bis zum 25. Sommerzeit)."""
    if not hasattr(time, "tzset"):
        pytest.skip("Zeitzone lässt sich hier nicht umstellen")
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Berlin"
    time.tzset()
    yield
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()


def test_now_and_now_iso_are_local_and_aware() -> None:
    current = now()
    assert current.tzinfo is not None
    assert abs(current - datetime.now(UTC)) < timedelta(seconds=5)
    stamp = now_iso()
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None
    assert parsed.microsecond == 0 and "." not in stamp  # nur Sekunden


def test_parse_iso_with_timezones() -> None:
    assert parse_iso("2026-10-12T14:30:00Z") == datetime(2026, 10, 12, 14, 30, tzinfo=UTC)
    shifted = parse_iso("  2026-10-12T14:30:00+02:00\n")
    assert shifted.utcoffset() == timedelta(hours=2)
    assert shifted == datetime(2026, 10, 12, 12, 30, tzinfo=UTC)
    with pytest.raises(ValueError):
        parse_iso("morgen früh")


@pytest.mark.usefixtures("berlin")
def test_parse_iso_naive_means_local_time() -> None:
    summer = parse_iso("2026-10-12T14:30")
    assert summer.utcoffset() == timedelta(hours=2)
    assert (summer.hour, summer.minute) == (14, 30)
    winter = parse_iso("2026-12-24 18:00:00")
    assert winter.utcoffset() == timedelta(hours=1)


@pytest.mark.usefixtures("berlin")
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (datetime(2026, 10, 12, 14, 30, tzinfo=timezone(timedelta(hours=2))), "Mo 12.10. 14:30"),
        (datetime(2026, 10, 12, 12, 30, tzinfo=UTC), "Mo 12.10. 14:30"),  # in Ortszeit
        (datetime(2026, 10, 11, 23, 30, tzinfo=UTC), "Mo 12.10. 01:30"),  # Datumsgrenze
        (datetime(2026, 10, 18, 9, 5), "So 18.10. 09:05"),  # ohne Zeitzone = Ortszeit
        (datetime(2026, 12, 24, 18, 0, tzinfo=UTC), "Do 24.12. 19:00"),  # Winterzeit
    ],
)
def test_human(value: datetime, expected: str) -> None:
    assert human(value) == expected
