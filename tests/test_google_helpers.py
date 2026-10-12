"""Gmail/Kalender-Hilfen ohne Netzwerk: HTML→Text, Nachrichtentext, Kontenverwaltung."""

from __future__ import annotations

import base64
import json
from datetime import datetime
from email import message_from_bytes, policy
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from jarvis.integrations.google import (
    GoogleAccounts,
    GoogleNotConfigured,
    extract_body,
    html_to_text,
)


def _b64(text: str) -> str:
    """Base64url wie in der Gmail-API – ohne Auffüll-Zeichen."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _token(accounts: GoogleAccounts, name: str, data: dict[str, Any] | str) -> Path:
    accounts.tokens_dir.mkdir(parents=True, exist_ok=True)
    path = accounts.tokens_dir / f"{name}.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def google(tmp_path: Path) -> GoogleAccounts:
    return GoogleAccounts(tmp_path / "google")


@pytest.fixture
def connected(google: GoogleAccounts) -> GoogleAccounts:
    """Zwei verbundene Konten; Dateinamen wie von ``_save`` erzeugt."""
    _token(google, "max@gmail.com", {"account": "max@gmail.com", "token": "x"})
    _token(google, "anna@firma.de", {"account": "anna@firma.de", "token": "y"})
    return google


# ---------------------------------------------------------------------- html_to_text


def test_html_to_text_drops_head_scripts_and_styles() -> None:
    html = (
        "<html><head><title>Newsletter</title><style>p { color: red }</style></head>"
        "<body><p>Hallo</p><script>alert('x')</script><p>Welt</p></body></html>"
    )
    assert html_to_text(html) == "Hallo\nWelt"


def test_html_to_text_decodes_entities() -> None:
    assert html_to_text("Preis &lt; 5 &euro; &amp; &#228;&uuml;") == "Preis < 5 € & äü"


def test_html_to_text_line_breaks_and_whitespace() -> None:
    assert html_to_text("a<br>b<br/>c") == "a\nb\nc"
    assert html_to_text("eins<br><br>zwei") == "eins\n\nzwei"  # Leerzeile bleibt
    assert html_to_text("<ul><li>eins</li><li>zwei</li></ul>") == "eins\nzwei"
    # Mehrfache Leerzeichen, leere Absätze und Leerzeilen werden zusammengefasst
    assert html_to_text("<p>x  \t  y</p><p></p><p></p><p>z</p>") == "x y\nz"
    assert html_to_text("<div>a</div>\n\n   \n<div>  b  </div>") == "a\n\nb"


def test_html_to_text_keeps_words_apart_after_blocks_and_cells() -> None:
    # Text nach einem schließenden Block und Tabellenzellen dürfen nicht verkleben
    assert html_to_text("<div>Hallo</div>Welt") == "Hallo\nWelt"
    table = (
        "<table><tr><td>Betrag:</td><td>42 €</td></tr>"
        "<tr><th>Datum:</th><td>heute</td></tr></table>"
    )
    assert html_to_text(table) == "Betrag: 42 €\nDatum: heute"


def test_html_to_text_keeps_trailing_text() -> None:
    # Früher blieb Text nach einem "&" ohne Leerzeichen im Parser hängen und fehlte.
    assert html_to_text("Kunde von AT&T") == "Kunde von AT&T"
    assert html_to_text("<p>Gruß</p>Ende &copy") == "Gruß\nEnde ©"


# ---------------------------------------------------------------------- extract_body


def test_extract_body_single_part() -> None:
    payload = {"mimeType": "text/plain", "body": {"data": _b64("Hallo Max,\nbis morgen!")}}
    assert extract_body(payload) == "Hallo Max,\nbis morgen!"


def test_extract_body_prefers_plain_over_html() -> None:
    payload = {
        "mimeType": "multipart/alternative",
        "body": {"size": 0},
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64("Nur Text – äöü")}},
            {"mimeType": "text/html", "body": {"data": _b64("<p>HTML-Version</p>")}},
        ],
    }
    assert extract_body(payload) == "Nur Text – äöü"


def test_extract_body_falls_back_to_html_in_nested_parts() -> None:
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/related",
                "parts": [
                    {
                        "mimeType": "text/html",
                        "body": {
                            "data": _b64("<style>x{}</style><p>Rechnung</p><p>&amp; mehr</p>")
                        },
                    },
                    {"mimeType": "image/png", "body": {"attachmentId": "img1", "size": 10}},
                ],
            },
            {"mimeType": "application/pdf", "filename": "a.pdf", "body": {"attachmentId": "a"}},
        ],
    }
    assert extract_body(payload) == "Rechnung\n& mehr"


def test_extract_body_joins_several_plain_parts_and_handles_empty() -> None:
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64("Teil 1")}},
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64("Teil 2")}},
                ],
            },
        ],
    }
    assert extract_body(payload) == "Teil 1\nTeil 2"
    assert extract_body({"mimeType": "multipart/mixed", "body": None, "parts": None}) == ""
    assert extract_body({"mimeType": "image/png", "body": {"data": _b64("png")}}) == ""


# ---------------------------------------------------------------------- Konten


def test_configured_depends_on_client_file(google: GoogleAccounts) -> None:
    assert google.client_file == google.base_dir / "credentials.json"
    assert google.configured() is False
    google.base_dir.mkdir(parents=True)
    google.client_file.write_text("{}", encoding="utf-8")
    assert google.configured() is True


def test_accounts_from_token_files(google: GoogleAccounts) -> None:
    assert google.accounts() == []  # Ordner existiert noch nicht
    _token(google, "b@x.de", {"account": "b@x.de"})
    _token(google, "a@x.de", {"token": "ohne Kontoangabe"})  # Dateiname als Ersatz
    _token(google, "kaputt", "{nicht json")
    (google.tokens_dir / "notiz.txt").write_text("egal", encoding="utf-8")
    assert google.accounts() == ["a@x.de", "b@x.de"]


def test_resolve_account(connected: GoogleAccounts) -> None:
    # Sortiert nach Dateiname: anna@firma.de zuerst
    assert connected.resolve_account(None) == "anna@firma.de"
    assert connected.resolve_account("") == "anna@firma.de"
    assert connected.resolve_account(" MAX@gmail.com ") == "max@gmail.com"
    assert connected.resolve_account("max") == "max@gmail.com"  # Präfix genügt
    with pytest.raises(GoogleNotConfigured, match="nicht verbunden") as info:
        connected.resolve_account("peter@web.de")
    assert "anna@firma.de, max@gmail.com" in str(info.value)


def test_resolve_account_without_accounts(google: GoogleAccounts) -> None:
    with pytest.raises(GoogleNotConfigured, match="google add"):
        google.resolve_account(None)


def test_calendar_account(connected: GoogleAccounts) -> None:
    assert connected.calendar_email() == "anna@firma.de"
    connected.calendar_account = "max@gmail.com"
    assert connected.calendar_email() == "max@gmail.com"
    connected.calendar_account = "weg@nirgends.de"
    with pytest.raises(GoogleNotConfigured):
        connected.calendar_email()


def test_remove_account(connected: GoogleAccounts) -> None:
    assert connected.remove_account("Max@Gmail.com") is True  # Groß/klein egal
    assert connected.accounts() == ["anna@firma.de"]
    assert connected.remove_account("max@gmail.com") is False


def test_add_account_requires_client_file(google: GoogleAccounts) -> None:
    with pytest.raises(GoogleNotConfigured, match="OAuth-Client fehlt"):
        google.add_account()


def test_credentials_for_unknown_account(google: GoogleAccounts) -> None:
    with pytest.raises(GoogleNotConfigured, match="nicht verbunden"):
        google.credentials("max@gmail.com")


# ---------------------------------------------------------------------- save_client


def test_save_client_accepts_desktop_client(google: GoogleAccounts) -> None:
    raw = json.dumps({"installed": {"client_id": "id", "client_secret": "s"}}).encode()
    google.save_client(b"\xef\xbb\xbf" + raw)  # mit BOM, wie Windows-Editoren speichern
    assert google.configured()
    assert json.loads(google.client_file.read_text(encoding="utf-8")) == {
        "installed": {"client_id": "id", "client_secret": "s"}
    }


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (json.dumps({"web": {"client_id": "id", "client_secret": "s"}}).encode(), "Desktop-App"),
        (b"\x89PNG kein json", "keine gültige JSON"),
        (b"[1, 2]", "Keine OAuth-Client-Datei"),
        (json.dumps({"installed": {"client_id": "id"}}).encode(), "Desktop-App"),
    ],
    ids=["web-client", "kein-json", "liste", "ohne-secret"],
)
def test_save_client_rejects_wrong_files(google: GoogleAccounts, raw: bytes, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        google.save_client(raw)
    assert not google.configured()


# ---------------------------------------------------------------------- Gmail/Kalender (gemockt)


@pytest.fixture
def service(connected: GoogleAccounts, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Ersetzt den Google-API-Client; merkt sich, für welches Konto er gebaut wurde."""
    fake = MagicMock(name="service")
    fake.built_for = []

    def build(api: str, version: str, account: str) -> MagicMock:
        fake.built_for.append((api, version, account))
        return fake

    monkeypatch.setattr(connected, "_service", build)
    return fake


def test_gmail_read_extracts_headers_and_body(
    connected: GoogleAccounts, service: MagicMock
) -> None:
    service.users().messages().get().execute.return_value = {
        "id": "m1",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "Chef <chef@firma.de>"},
                {"name": "to", "value": "max@gmail.com"},
                {"name": "Subject", "value": "Meeting"},
            ],
            "body": {"data": _b64("Um 10 Uhr.")},
        },
    }
    mail = connected.gmail_read("max@gmail.com", "m1")
    assert mail == {
        "id": "m1",
        "account": "max@gmail.com",
        "from": "Chef <chef@firma.de>",
        "to": "max@gmail.com",  # Kopfzeilen ohne Groß/klein-Unterschied
        "subject": "Meeting",
        "date": "",
        "body": "Um 10 Uhr.",
    }
    assert service.built_for == [("gmail", "v1", "max@gmail.com")]


def test_gmail_search_maps_metadata(connected: GoogleAccounts, service: MagicMock) -> None:
    service.users().messages().list().execute.return_value = {
        "messages": [{"id": "a"}, {"id": "b"}]
    }
    service.users().messages().get().execute.side_effect = [
        {
            "id": "a",
            "snippet": "Hallo",
            "labelIds": ["INBOX", "UNREAD"],
            "payload": {"headers": [{"name": "Subject", "value": "Neu"}]},
        },
        {"id": "b", "payload": {"headers": []}},
    ]
    results = connected.gmail_search("max@gmail.com", "is:unread", max_results=5)
    assert [r["id"] for r in results] == ["a", "b"]
    assert results[0]["unread"] is True and results[0]["subject"] == "Neu"
    assert results[1]["unread"] is False and results[1]["subject"] == "(ohne Betreff)"
    service.users().messages().list.assert_called_with(userId="me", q="is:unread", maxResults=5)


def test_gmail_draft_builds_mime_message(connected: GoogleAccounts, service: MagicMock) -> None:
    service.users().drafts().create().execute.return_value = {"id": "d1"}
    assert connected.gmail_draft("max@gmail.com", "anna@firma.de", "Grüße", "Hallo Anna") == "d1"
    body = service.users().drafts().create.call_args.kwargs["body"]
    raw = base64.urlsafe_b64decode(body["message"]["raw"])
    message = message_from_bytes(raw, policy=policy.default)
    assert message["To"] == "anna@firma.de"
    assert message["From"] == "max@gmail.com"
    assert message["Subject"] == "Grüße"
    assert message.get_content().strip() == "Hallo Anna"  # type: ignore[attr-defined]


def test_gmail_send_rejects_invalid_recipient(
    connected: GoogleAccounts, service: MagicMock
) -> None:
    with pytest.raises(ValueError, match="Empfängeradresse"):
        connected.gmail_send("max@gmail.com", "kein-empfaenger", "x", "y")
    assert service.built_for == []  # gar nicht erst verbunden


def test_calendar_events_uses_calendar_account(
    connected: GoogleAccounts, service: MagicMock
) -> None:
    connected.calendar_account = "max"
    service.events().list().execute.return_value = {
        "items": [
            {
                "id": "e1",
                "summary": "Zahnarzt",
                "start": {"dateTime": "2026-10-13T09:00:00+02:00"},
                "end": {"dateTime": "2026-10-13T10:00:00+02:00"},
                "description": "x" * 600,
            },
            {"id": "e2", "start": {"date": "2026-10-14"}, "end": {"date": "2026-10-15"}},
        ]
    }
    events = connected.calendar_events(days=3, max_results=5)
    assert service.built_for == [("calendar", "v3", "max@gmail.com")]
    assert events[0]["title"] == "Zahnarzt" and events[0]["all_day"] is False
    assert len(events[0]["description"]) == 500
    assert events[1] == {
        "id": "e2",
        "title": "(ohne Titel)",
        "start": "2026-10-14",
        "end": "2026-10-15",
        "all_day": True,
        "location": "",
        "description": "",
    }
    kwargs = service.events().list.call_args.kwargs
    assert kwargs["calendarId"] == "primary" and kwargs["maxResults"] == 5
    span = datetime.fromisoformat(kwargs["timeMax"]) - datetime.fromisoformat(kwargs["timeMin"])
    assert span.days == 3


def test_calendar_create(connected: GoogleAccounts, service: MagicMock) -> None:
    service.events().insert().execute.return_value = {"id": "n1", "htmlLink": "https://cal/n1"}
    start = datetime(2026, 10, 20, 15, 0).astimezone()
    end = datetime(2026, 10, 20, 16, 0).astimezone()
    result = connected.calendar_create("Training", start, end, location="Halle")
    assert result == {"id": "n1", "link": "https://cal/n1"}
    body = service.events().insert.call_args.kwargs["body"]
    assert body["summary"] == "Training" and body["location"] == "Halle"
    assert body["start"] == {"dateTime": start.isoformat()}
    assert service.built_for == [("calendar", "v3", "anna@firma.de")]
