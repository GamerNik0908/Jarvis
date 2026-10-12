"""Gemeinsame Grundtypen, die von mehreren Modulen verwendet werden."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal


class TaskKind(StrEnum):
    """Art eines Teilschritts – danach wählt der Router das Modell aus."""

    CHAT = "chat"
    PLANNING = "planning"
    CODE = "code"
    REVIEW = "review"
    SIMPLE = "simple"
    SUMMARIZE = "summarize"


#: Wie gründlich ein Modell nachdenken soll (beeinflusst Qualität und Kosten).
Effort = Literal["low", "medium", "high", "xhigh", "max"]

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
