from __future__ import annotations

from datetime import datetime

from jarvis.core.app import german_date
from jarvis.core.conversation import Conversation


def test_trims_by_message_count_and_starts_with_user() -> None:
    conv = Conversation(max_messages=3, max_chars=10_000)
    for i in range(3):
        conv.add_user(f"frage {i}")
        conv.add_assistant(f"antwort {i}")
    messages = conv.messages
    assert len(messages) <= 3
    assert messages[0].role == "user"
    assert messages[-1].content == "antwort 2"


def test_trims_by_characters_but_keeps_latest() -> None:
    conv = Conversation(max_messages=50, max_chars=1_000)
    conv.add_user("a" * 600)
    conv.add_assistant("b" * 600)
    conv.add_user("c" * 2_000)
    assert [m.content[0] for m in conv.messages] == ["c"]


def test_rollback_only_removes_unanswered_user_message() -> None:
    conv = Conversation()
    conv.add_user("hallo")
    conv.add_assistant("hi")
    conv.rollback_last_user()
    assert len(conv) == 2
    conv.add_user("noch was")
    conv.rollback_last_user()
    assert len(conv) == 2


def test_german_date_is_locale_independent() -> None:
    assert german_date(datetime(2026, 10, 12)) == "Montag, 12. Oktober 2026"
    assert german_date(datetime(2026, 3, 1)) == "Sonntag, 1. März 2026"
