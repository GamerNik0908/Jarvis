"""Gmail (mehrere Konten) und Google Kalender über die offiziellen Google-APIs.

Einmalige Einrichtung (kostenlos, ca. 10 Minuten – Anleitung im README):

1. In der Google Cloud Console ein Projekt anlegen, Gmail- und Calendar-API
   aktivieren, einen OAuth-Client vom Typ "Desktop-App" erstellen und die
   JSON-Datei als ``<Datenordner>\\google\\credentials.json`` speichern.
2. Pro Gmail-Konto ``jarvis google add`` ausführen und im Browser zustimmen.

Die Zugangsdaten pro Konto liegen in ``<Datenordner>\\google\\tokens\\``.
JARVIS darf diese Dateien selbst nicht lesen (siehe ``security/paths.py``).
"""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import parseaddr
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.events",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


class GoogleNotConfigured(RuntimeError):
    """Google ist (noch) nicht eingerichtet."""


def _safe_name(email: str) -> str:
    return re.sub(r"[^a-zA-Z0-9@._-]", "_", email.lower())


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "head"}:
            self._skip += 1
        elif tag in {"br", "p", "div", "tr", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "head"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    text = "".join(parser.parts)
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def extract_body(payload: dict[str, Any]) -> str:
    """Holt den Text einer Gmail-Nachricht (bevorzugt text/plain)."""
    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        body = part.get("body", {}) or {}
        if body.get("data"):
            if mime == "text/plain":
                plain.append(_decode(body["data"]))
            elif mime == "text/html":
                html.append(_decode(body["data"]))
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    if plain:
        return "\n".join(plain).strip()
    if html:
        return html_to_text("\n".join(html))
    return ""


def _header(headers: list[dict[str, str]], name: str) -> str:
    return next(
        (h.get("value", "") for h in headers if h.get("name", "").lower() == name.lower()), ""
    )


class GoogleAccounts:
    """Verwaltet die verbundenen Google-Konten und ihre API-Clients."""

    def __init__(self, base_dir: Path, *, calendar_account: str = "") -> None:
        self.base_dir = base_dir
        self.tokens_dir = base_dir / "tokens"
        self.client_file = base_dir / "credentials.json"
        self.calendar_account = calendar_account

    def configured(self) -> bool:
        return self.client_file.is_file()

    def accounts(self) -> list[str]:
        if not self.tokens_dir.is_dir():
            return []
        result = []
        for path in sorted(self.tokens_dir.glob("*.json")):
            try:
                result.append(
                    str(json.loads(path.read_text(encoding="utf-8")).get("account") or path.stem)
                )
            except (OSError, json.JSONDecodeError):
                continue
        return result

    def resolve_account(self, account: str | None) -> str:
        accounts = self.accounts()
        if not accounts:
            raise GoogleNotConfigured("Kein Google-Konto verbunden – 'jarvis google add' ausführen")
        if not account:
            return accounts[0]
        wanted = account.lower().strip()
        for candidate in accounts:
            if candidate.lower() == wanted or candidate.lower().startswith(wanted):
                return candidate
        raise GoogleNotConfigured(
            f"Konto '{account}' ist nicht verbunden. Verbunden: {', '.join(accounts)}"
        )

    def calendar_email(self) -> str:
        return self.resolve_account(self.calendar_account or None)

    def add_account(self) -> str:
        """Öffnet den Browser für die Anmeldung und speichert das Token."""
        from google_auth_oauthlib.flow import InstalledAppFlow

        if not self.configured():
            raise GoogleNotConfigured(f"OAuth-Client fehlt: {self.client_file}")
        flow = InstalledAppFlow.from_client_secrets_file(str(self.client_file), SCOPES)
        creds = flow.run_local_server(
            port=0,
            open_browser=True,
            prompt="consent select_account",
            authorization_prompt_message="Bitte im Browser mit dem gewünschten Google-Konto anmelden: {url}",
            success_message="JARVIS ist verbunden. Du kannst dieses Fenster schließen.",
        )
        from googleapiclient.discovery import build

        profile = (
            build("gmail", "v1", credentials=creds, cache_discovery=False)
            .users()
            .getProfile(userId="me")
            .execute()
        )
        email = str(profile["emailAddress"])
        self._save(email, creds)
        return email

    def remove_account(self, email: str) -> bool:
        path = self.tokens_dir / f"{_safe_name(email)}.json"
        if path.is_file():
            path.unlink()
            return True
        return False

    def _save(self, email: str, creds: Any) -> None:
        self.tokens_dir.mkdir(parents=True, exist_ok=True)
        data = json.loads(creds.to_json())
        data["account"] = email
        (self.tokens_dir / f"{_safe_name(email)}.json").write_text(
            json.dumps(data), encoding="utf-8"
        )

    def credentials(self, email: str) -> Any:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        path = self.tokens_dir / f"{_safe_name(email)}.json"
        if not path.is_file():
            raise GoogleNotConfigured(f"Konto '{email}' ist nicht verbunden")
        info = json.loads(path.read_text(encoding="utf-8"))
        creds = Credentials.from_authorized_user_info(info, SCOPES)  # type: ignore[no-untyped-call]
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                self._save(email, creds)
            else:
                raise GoogleNotConfigured(
                    f"Anmeldung für '{email}' abgelaufen – 'jarvis google add' erneut ausführen"
                )
        return creds

    def _service(self, api: str, version: str, email: str) -> Any:
        from googleapiclient.discovery import build

        return build(api, version, credentials=self.credentials(email), cache_discovery=False)

    # ------------------------------------------------------------------ Gmail

    def gmail_search(self, email: str, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        service = self._service("gmail", "v1", email)
        listing = (
            service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        )
        results = []
        for item in listing.get("messages", []) or []:
            message = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=item["id"],
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
                .execute()
            )
            headers = message.get("payload", {}).get("headers", [])
            results.append(
                {
                    "id": message["id"],
                    "account": email,
                    "from": _header(headers, "From"),
                    "subject": _header(headers, "Subject") or "(ohne Betreff)",
                    "date": _header(headers, "Date"),
                    "snippet": message.get("snippet", ""),
                    "unread": "UNREAD" in (message.get("labelIds") or []),
                }
            )
        return results

    def gmail_read(self, email: str, message_id: str) -> dict[str, Any]:
        service = self._service("gmail", "v1", email)
        message = (
            service.users().messages().get(userId="me", id=message_id, format="full").execute()
        )
        payload = message.get("payload", {})
        headers = payload.get("headers", [])
        return {
            "id": message_id,
            "account": email,
            "from": _header(headers, "From"),
            "to": _header(headers, "To"),
            "subject": _header(headers, "Subject"),
            "date": _header(headers, "Date"),
            "body": extract_body(payload),
        }

    def gmail_unread_count(self, email: str) -> int:
        service = self._service("gmail", "v1", email)
        label = service.users().labels().get(userId="me", id="INBOX").execute()
        return int(label.get("messagesUnread", 0))

    @staticmethod
    def _mime(
        sender: str, to: str, subject: str, body: str, reply_to_id: str | None = None
    ) -> dict[str, Any]:
        message = EmailMessage()
        message["To"] = to
        message["From"] = sender
        message["Subject"] = subject
        message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        data: dict[str, Any] = {"raw": raw}
        if reply_to_id:
            data["threadId"] = reply_to_id
        return data

    def gmail_draft(self, email: str, to: str, subject: str, body: str) -> str:
        service = self._service("gmail", "v1", email)
        draft = (
            service.users()
            .drafts()
            .create(userId="me", body={"message": self._mime(email, to, subject, body)})
            .execute()
        )
        return str(draft.get("id"))

    def gmail_send(self, email: str, to: str, subject: str, body: str) -> str:
        if "@" not in parseaddr(to)[1]:
            raise ValueError(f"Ungültige Empfängeradresse: {to}")
        service = self._service("gmail", "v1", email)
        sent = (
            service.users()
            .messages()
            .send(userId="me", body=self._mime(email, to, subject, body))
            .execute()
        )
        return str(sent.get("id"))

    # ------------------------------------------------------------------ Kalender

    def calendar_events(self, days: int = 7, max_results: int = 30) -> list[dict[str, Any]]:
        email = self.calendar_email()
        service = self._service("calendar", "v3", email)
        start = datetime.now().astimezone()
        end = start + timedelta(days=days)
        listing = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=max_results,
            )
            .execute()
        )
        events = []
        for item in listing.get("items", []) or []:
            start_info = item.get("start", {})
            end_info = item.get("end", {})
            events.append(
                {
                    "id": item.get("id"),
                    "title": item.get("summary", "(ohne Titel)"),
                    "start": start_info.get("dateTime") or start_info.get("date"),
                    "end": end_info.get("dateTime") or end_info.get("date"),
                    "all_day": "date" in start_info,
                    "location": item.get("location", ""),
                    "description": (item.get("description") or "")[:500],
                }
            )
        return events

    def calendar_create(
        self,
        title: str,
        start: datetime,
        end: datetime,
        *,
        description: str = "",
        location: str = "",
    ) -> dict[str, Any]:
        service = self._service("calendar", "v3", self.calendar_email())
        body = {
            "summary": title,
            "description": description,
            "location": location,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }
        created = service.events().insert(calendarId="primary", body=body).execute()
        return {"id": created.get("id"), "link": created.get("htmlLink")}

    def calendar_delete(self, event_id: str) -> None:
        service = self._service("calendar", "v3", self.calendar_email())
        service.events().delete(calendarId="primary", eventId=event_id).execute()
