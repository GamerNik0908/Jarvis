"""Gmail (mehrere Konten) und Google Kalender als Werkzeuge.

E-Mail- und Kalenderinhalte gelten als privat: Sobald sie im Gespräch sind,
nutzt JARVIS nur noch vertrauenswürdige Modelle (lokal oder dein Claude-Abo).
Senden und Löschen braucht immer deine Bestätigung.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from jarvis.core.timeutil import parse_iso
from jarvis.integrations.google import GoogleNotConfigured
from jarvis.plugins.base import ToolContext, ToolResult, obj, tool
from jarvis.security.permissions import Level


def _needs_google(ctx: ToolContext) -> str | None:
    google = ctx.app.google
    if not google.configured():
        return "Google nicht eingerichtet (siehe README: Gmail & Kalender)"
    if not google.accounts():
        return "kein Google-Konto verbunden ('jarvis google add')"
    return None


ACCOUNT = {"type": "string", "description": "E-mail address of the account; empty = first account"}


@tool(
    name="gmail_accounts",
    description="List the connected Gmail accounts with their unread inbox counts.",
    parameters=obj({}),
    category="e-mail",
    available=_needs_google,
)
async def gmail_accounts(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    google = ctx.app.google
    lines = []
    for email in google.accounts():
        try:
            unread = await asyncio.to_thread(google.gmail_unread_count, email)
            lines.append(f"{email}: {unread} ungelesen")
        except Exception as exc:
            lines.append(f"{email}: nicht abrufbar ({exc})")
    return ToolResult("\n".join(lines), sensitive=True)


@tool(
    name="gmail_search",
    description=(
        "Search e-mails with Gmail syntax (e.g. 'is:unread', 'from:amazon newer_than:7d', "
        "'subject:Rechnung'). Searches one account, or all accounts if account='all'."
    ),
    parameters=obj(
        {
            "query": {"type": "string"},
            "account": ACCOUNT,
            "max_results": {"type": "integer", "description": "Default 10"},
        },
        ["query"],
    ),
    category="e-mail",
    available=_needs_google,
    timeout_s=90,
)
async def gmail_search(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    google = ctx.app.google
    limit = max(1, min(int(args.get("max_results") or 10), 30))
    account = str(args.get("account") or "")
    try:
        accounts = (
            google.accounts()
            if account.lower() == "all"
            else [google.resolve_account(account or None)]
        )
    except GoogleNotConfigured as exc:
        return ToolResult.error(str(exc))
    found: list[dict[str, Any]] = []
    for email in accounts:
        try:
            found += await asyncio.to_thread(google.gmail_search, email, str(args["query"]), limit)
        except Exception as exc:
            return ToolResult.error(f"Gmail ({email}) nicht abrufbar: {exc}")
    if not found:
        return ToolResult("Keine passenden E-Mails gefunden.", sensitive=True)
    lines = [
        f"[{m['account']}] id={m['id']} {'● ' if m['unread'] else ''}{m['date']}\n"
        f"  Von: {m['from']}\n  Betreff: {m['subject']}\n  {m['snippet']}"
        for m in found
    ]
    return ToolResult("\n".join(lines), sensitive=True, data={"messages": found})


@tool(
    name="gmail_read",
    description="Read the full text of an e-mail (id from gmail_search).",
    parameters=obj({"id": {"type": "string"}, "account": ACCOUNT}, ["id"]),
    category="e-mail",
    available=_needs_google,
)
async def gmail_read(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    google = ctx.app.google
    try:
        email = google.resolve_account(str(args.get("account") or "") or None)
        message = await asyncio.to_thread(google.gmail_read, email, str(args["id"]))
    except Exception as exc:
        return ToolResult.error(f"E-Mail nicht abrufbar: {exc}")
    text = (
        f"Von: {message['from']}\nAn: {message['to']}\nDatum: {message['date']}\n"
        f"Betreff: {message['subject']}\n\n{message['body'][:15000]}"
    )
    return ToolResult(text, sensitive=True)


@tool(
    name="gmail_draft",
    description="Create a draft e-mail (not sent). The user can review it in Gmail.",
    parameters=obj(
        {
            "to": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "account": ACCOUNT,
        },
        ["to", "subject", "body"],
    ),
    category="e-mail",
    available=_needs_google,
)
async def gmail_draft(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    google = ctx.app.google
    try:
        email = google.resolve_account(str(args.get("account") or "") or None)
        await asyncio.to_thread(
            google.gmail_draft, email, str(args["to"]), str(args["subject"]), str(args["body"])
        )
    except Exception as exc:
        return ToolResult.error(f"Entwurf fehlgeschlagen: {exc}")
    return ToolResult(f"Entwurf in {email} gespeichert (an {args['to']}).", sensitive=True)


@tool(
    name="gmail_send",
    description="Send an e-mail. The user must confirm. Prefer gmail_draft if unsure.",
    parameters=obj(
        {
            "to": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "account": ACCOUNT,
        },
        ["to", "subject", "body"],
    ),
    level=Level.CONFIRM,
    category="e-mail",
    available=_needs_google,
    summary=lambda a: (
        f"E-Mail senden{(' von ' + a['account']) if a.get('account') else ''} an {a.get('to')}\n"
        f"Betreff: {a.get('subject')}\n\n{str(a.get('body', ''))[:600]}"
    ),
)
async def gmail_send(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    google = ctx.app.google
    try:
        email = google.resolve_account(str(args.get("account") or "") or None)
        await asyncio.to_thread(
            google.gmail_send, email, str(args["to"]), str(args["subject"]), str(args["body"])
        )
    except Exception as exc:
        return ToolResult.error(f"Senden fehlgeschlagen: {exc}")
    return ToolResult(f"E-Mail von {email} an {args['to']} gesendet.", sensitive=True)


@tool(
    name="calendar_events",
    description="List upcoming calendar events for the next N days (default 7).",
    parameters=obj({"days": {"type": "integer"}}),
    category="kalender",
    available=_needs_google,
)
async def calendar_events(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    days = max(1, min(int(args.get("days") or 7), 60))
    try:
        events = await asyncio.to_thread(ctx.app.google.calendar_events, days)
    except Exception as exc:
        return ToolResult.error(f"Kalender nicht abrufbar: {exc}")
    if not events:
        return ToolResult(f"Keine Termine in den nächsten {days} Tagen.", sensitive=True)
    lines = [
        f"id={e['id']} {e['start']}{' (ganztägig)' if e['all_day'] else ''}: {e['title']}"
        + (f" @ {e['location']}" if e["location"] else "")
        for e in events
    ]
    return ToolResult("\n".join(lines), sensitive=True, data={"events": events})


@tool(
    name="calendar_create",
    description="Create a calendar event. start/end as ISO local date-time; end defaults to start + 1 hour.",
    parameters=obj(
        {
            "title": {"type": "string"},
            "start": {"type": "string"},
            "end": {"type": "string"},
            "description": {"type": "string"},
            "location": {"type": "string"},
        },
        ["title", "start"],
    ),
    category="kalender",
    available=_needs_google,
)
async def calendar_create(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    try:
        start = parse_iso(str(args["start"]))
        end = parse_iso(str(args["end"])) if args.get("end") else start + timedelta(hours=1)
    except ValueError:
        return ToolResult.error("Zeitangabe nicht verstanden – ISO-Format wie 2026-10-14T15:00")
    try:
        created = await asyncio.to_thread(
            ctx.app.google.calendar_create,
            str(args["title"]),
            start,
            end,
            description=str(args.get("description") or ""),
            location=str(args.get("location") or ""),
        )
    except Exception as exc:
        return ToolResult.error(f"Termin konnte nicht angelegt werden: {exc}")
    return ToolResult(f"Termin '{args['title']}' am {start:%d.%m.%Y %H:%M} angelegt.", data=created)


@tool(
    name="calendar_delete",
    description="Delete a calendar event by id (from calendar_events). Needs confirmation.",
    parameters=obj(
        {
            "id": {"type": "string"},
            "title": {"type": "string", "description": "for the confirmation"},
        },
        ["id"],
    ),
    level=Level.CONFIRM,
    category="kalender",
    available=_needs_google,
    summary=lambda a: f"Termin löschen: {a.get('title') or a.get('id')}",
)
async def calendar_delete(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    try:
        await asyncio.to_thread(ctx.app.google.calendar_delete, str(args["id"]))
    except Exception as exc:
        return ToolResult.error(f"Löschen fehlgeschlagen: {exc}")
    return ToolResult("Termin gelöscht.")
