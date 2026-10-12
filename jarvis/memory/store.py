"""Langzeitgedächtnis: Fakten, Vorlieben und Erkenntnisse über dich und deine Aufgaben.

Gespeichert in SQLite. Gesucht wird per Volltextsuche (FTS5) und – wenn ein
Embedding-Modell verfügbar ist – zusätzlich semantisch über Vektoren.
Standardmäßig wird dafür ein lokales Ollama-Modell genutzt, damit persönliche
Fakten deinen PC nicht verlassen.
"""

from __future__ import annotations

import logging
import math
import re
import struct
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from jarvis.core.db import Database
from jarvis.core.timeutil import now_iso

log = logging.getLogger(__name__)

EmbedFn = Callable[[Sequence[str]], Awaitable[list[list[float]]]]


@dataclass(frozen=True)
class Memory:
    id: int
    text: str
    category: str
    created: str
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "category": self.category,
            "created": self.created,
        }


@dataclass
class Embedder:
    name: str
    embed: EmbedFn


def _pack(vector: Sequence[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def _unpack(blob: bytes) -> list[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"{count}f", blob))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _fts_query(text: str) -> str:
    words = re.findall(r"\w{2,}", text.lower())
    return " OR ".join(f'"{w}"' for w in words[:12])


class MemoryStore:
    def __init__(self, db: Database, embedder: Embedder | None = None) -> None:
        self._db = db
        self.embedder = embedder
        self._fts = db.has_table("memories_fts")

    async def _embed(self, texts: Sequence[str]) -> list[list[float]] | None:
        if self.embedder is None:
            return None
        try:
            return await self.embedder.embed(texts)
        except Exception as exc:
            log.info("Embedding nicht verfügbar (%s) – nutze Volltextsuche", exc)
            return None

    async def remember(self, text: str, *, category: str = "fakt", source: str = "chat") -> int:
        text = text.strip()
        if not text:
            raise ValueError("Leerer Eintrag")
        existing = self._db.query_one(
            "SELECT id FROM memories WHERE lower(text) = lower(?)", (text,)
        )
        if existing is not None:
            return int(existing[0])
        vectors = await self._embed([text])
        blob = _pack(vectors[0]) if vectors else None
        return self._db.execute(
            "INSERT INTO memories (text, category, source, created, embedding, embedding_model) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                text,
                category,
                source,
                now_iso(),
                blob,
                self.embedder.name if blob and self.embedder else None,
            ),
        )

    def forget(self, memory_id: int) -> bool:
        before = self._db.query_one("SELECT 1 FROM memories WHERE id = ?", (memory_id,))
        if before is None:
            return False
        self._db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        return True

    def all(self, limit: int = 200) -> list[Memory]:
        rows = self._db.query(
            "SELECT id, text, category, created FROM memories ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [Memory(r["id"], r["text"], r["category"], r["created"]) for r in rows]

    def count(self) -> int:
        row = self._db.query_one("SELECT COUNT(*) FROM memories")
        return int(row[0]) if row else 0

    async def recall(self, query: str, *, limit: int = 8) -> list[Memory]:
        scores: dict[int, float] = {}
        texts: dict[int, tuple[str, str, str]] = {}
        for memory in self._keyword_search(query, limit * 2):
            scores[memory.id] = max(scores.get(memory.id, 0.0), memory.score)
            texts[memory.id] = (memory.text, memory.category, memory.created)
        vectors = await self._embed([query]) if self.embedder else None
        if vectors:
            query_vec = vectors[0]
            rows = self._db.query(
                "SELECT id, text, category, created, embedding FROM memories "
                "WHERE embedding IS NOT NULL AND embedding_model = ?",
                (self.embedder.name if self.embedder else "",),
            )
            for row in rows:
                similarity = cosine(query_vec, _unpack(row["embedding"]))
                if similarity < 0.35:
                    continue
                scores[row["id"]] = max(scores.get(row["id"], 0.0), similarity)
                texts[row["id"]] = (row["text"], row["category"], row["created"])
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]
        return [Memory(mid, *texts[mid], score=score) for mid, score in ranked]

    def _keyword_search(self, query: str, limit: int) -> list[Memory]:
        if self._fts:
            match = _fts_query(query)
            if not match:
                return []
            try:
                rows = self._db.query(
                    "SELECT m.id, m.text, m.category, m.created, bm25(memories_fts) AS rank "
                    "FROM memories_fts JOIN memories m ON m.id = memories_fts.rowid "
                    "WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?",
                    (match, limit),
                )
            except Exception:  # z. B. Sonderzeichen in der Anfrage
                log.debug("FTS-Suche fehlgeschlagen", exc_info=True)
                rows = []
            return [
                Memory(r["id"], r["text"], r["category"], r["created"], score=0.5) for r in rows
            ]
        words = re.findall(r"\w{3,}", query.lower())[:5]
        if not words:
            return []
        clause = " OR ".join("lower(text) LIKE ?" for _ in words)
        rows = self._db.query(
            f"SELECT id, text, category, created FROM memories WHERE {clause} LIMIT ?",
            (*[f"%{w}%" for w in words], limit),
        )
        return [Memory(r["id"], r["text"], r["category"], r["created"], score=0.4) for r in rows]
