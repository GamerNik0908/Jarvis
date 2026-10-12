"""Robustes Auslesen von JSON aus Modellantworten (die oft Text drumherum schreiben)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

_DECODER = json.JSONDecoder()


def _structures(text: str, opener: str) -> Iterator[tuple[Any, int, int]]:
    """Alle vollständigen JSON-Strukturen, die mit ``opener`` beginnen: (Wert, Start, Ende)."""
    start = text.find(opener)
    while start != -1:
        try:
            value, end = _DECODER.raw_decode(text, start)
        except json.JSONDecodeError:
            pass
        else:
            yield value, start, end
        start = text.find(opener, start + 1)


def extract_json(text: str) -> Any:
    """Findet das erste vollständige JSON-Objekt bzw. -Array in ``text``.

    Objekte haben Vorrang vor Listen, die vorher im Fließtext stehen (etwa ``[1]``).
    Ist das Objekt aber nur ein Element einer Liste, wird die ganze Liste geliefert.
    """
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(text)
    for candidate in candidates:
        candidate = candidate.strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        found = next(_structures(candidate, "{"), None)
        if found is not None:
            value, start, end = found
            for array, array_start, array_end in _structures(candidate, "["):
                if array_start > start:
                    break
                if array_end >= end:
                    return array
            return value
        array = next(_structures(candidate, "["), None)
        if array is not None:
            return array[0]
    raise ValueError("Keine gültige JSON-Struktur in der Antwort gefunden")
