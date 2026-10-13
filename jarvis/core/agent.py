"""Der Agent-Loop: Planen → Werkzeug wählen → Ausführen → Ergebnis prüfen → Weiter.

Das Modell entscheidet in jedem Schritt selbst, ob es ein Werkzeug braucht
(Function Calling). JARVIS führt die gewünschten Werkzeuge aus – immer über
Berechtigungen, Not-Aus und Protokoll – und gibt die Ergebnisse zurück, bis
das Modell eine fertige Antwort liefert.

Bei Claude Code passiert die Werkzeug-Schleife innerhalb von Claude Code; die
Werkzeuge werden über die MCP-Brücke trotzdem hier ausgeführt.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from jarvis.core.types import TaskKind
from jarvis.plugins.base import ToolContext, ToolResult
from jarvis.router.base import ChatMessage, ChatResponse, ToolSpec
from jarvis.router.errors import NoModelAvailableError, RouterError
from jarvis.security.paths import expand_user

if TYPE_CHECKING:
    from jarvis.core.app import JarvisApp

log = logging.getLogger(__name__)

TOOL_GUIDE = """
Werkzeuge: Du kannst Werkzeuge benutzen (Dateien, Programme, Internet, Wetter, Bilder, Erinnerungen,
Gedächtnis, E-Mail, Kalender, Projekte, Selbstverbesserung …). Nutze sie aktiv, statt zu raten oder
zu behaupten, du könntest etwas nicht. Regeln:
- Bilder: image_generate (neues Bild erzeugen) oder image_search (echte Fotos finden). Die Bilder
  werden automatisch angezeigt – setze keine Bild-Links in deine Antwort.
- Wünscht sich der Benutzer eine neue Fähigkeit von JARVIS selbst (z. B. "bau eine Druckfunktion",
  "du sollst auch X können") oder meldet einen Fehler von JARVIS: jarvis_improve.
- Große Vorhaben, die viele Schritte oder Tage brauchen: project_create.
- Langfristig wichtige Infos über den Benutzer: memory_remember.
- Riskante Aktionen legt das System automatisch zur Bestätigung vor – frage nicht zusätzlich nach.
- Wenn ein Werkzeug fehlschlägt, erkläre kurz warum und biete eine Alternative an.
""".strip()

VOICE_GUIDE = (
    "Diese Antwort wird vorgelesen: Antworte kurz (meist 1–3 Sätze), natürlich gesprochen, "
    "ohne Markdown, Listen oder Emojis."
)

_FINAL_NUDGE = (
    "Du hast die maximale Anzahl an Werkzeug-Schritten erreicht. Antworte dem Benutzer jetzt "
    "ohne weitere Werkzeuge mit dem bisherigen Stand."
)


@dataclass
class AgentEvent:
    kind: Literal["text", "tool", "final", "error"]
    text: str = ""
    response: ChatResponse | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    text: str
    response: ChatResponse | None
    tools: list[dict[str, Any]]
    images: list[dict[str, Any]]
    error: str | None = None


def merge_roles(messages: Sequence[ChatMessage]) -> list[ChatMessage]:
    """Fasst aufeinanderfolgende Nachrichten derselben Rolle zusammen und beginnt mit ``user``."""
    merged: list[ChatMessage] = []
    for message in messages:
        if merged and merged[-1].role == message.role and message.role in ("user", "assistant"):
            previous = merged[-1]
            merged[-1] = ChatMessage(
                role=previous.role, content=f"{previous.content}\n\n{message.content}"
            )
        else:
            merged.append(message)
    while merged and merged[0].role != "user":
        merged.pop(0)
    return merged


def german_time(value: datetime) -> str:
    from jarvis.core.app import german_date

    return f"{german_date(value)}, {value:%H:%M} Uhr"


class Agent:
    def __init__(self, app: JarvisApp) -> None:
        self.app = app

    def history(self, conversation_id: str) -> list[ChatMessage]:
        config = self.app.config.conversation
        stored = self.app.conversations.messages(conversation_id, limit=config.max_messages)
        messages = [
            ChatMessage(role="user" if m.role == "user" else "assistant", content=m.content)
            for m in stored
            if m.role in ("user", "assistant") and m.content.strip()
        ]
        total = 0
        kept: list[ChatMessage] = []
        for message in reversed(messages):
            total += len(message.content)
            if kept and total > config.max_chars:
                break
            kept.append(message)
        return merge_roles(list(reversed(kept)))

    async def system_prompt(
        self, user_text: str, *, source: str, extra: str | None = None, with_tools: bool = True
    ) -> str:
        parts = [self.app.system_prompt()]
        parts.append(f"Aktuelle Zeit: {german_time(datetime.now())}.")
        if self.app.config.assistant.home_location:
            parts.append(f"Wohnort des Benutzers: {self.app.config.assistant.home_location}.")
        if self.app.config.memory.enabled and self.app.config.memory.max_context_facts:
            try:
                facts = await self.app.memory.recall(
                    user_text, limit=self.app.config.memory.max_context_facts
                )
            except Exception:
                log.debug("Gedächtnisabfrage fehlgeschlagen", exc_info=True)
                facts = []
            if facts:
                parts.append(
                    "Was du über den Benutzer weißt (Gedächtnis):\n"
                    + "\n".join(f"- {f.text}" for f in facts)
                )
        accounts = self.app.google.accounts() if self.app.google.configured() else []
        if accounts:
            parts.append("Verbundene Gmail-Konten: " + ", ".join(accounts))
        if with_tools:
            parts.append(TOOL_GUIDE)
            parts.append(
                f"Dein Arbeitsordner (dort darfst du frei Dateien anlegen): {self.app.workspace}\n"
                f"Benutzerordner: {Path.home()}\n"
                # Echte Pfade: Bei OneDrive-Sicherung liegen sie nicht unter dem Benutzerordner.
                f"Desktop: {expand_user('~/Desktop')} · Dokumente: {expand_user('~/Documents')}"
            )
        if source == "voice":
            parts.append(VOICE_GUIDE)
        if extra:
            parts.append(extra)
        return "\n\n".join(parts)

    async def run(
        self,
        conversation_id: str,
        user_text: str,
        *,
        task: TaskKind = TaskKind.CHAT,
        source: str = "chat",
        pinned_model: str | None = None,
        project_id: str | None = None,
        system_extra: str | None = None,
        use_tools: bool = True,
        tool_names: Sequence[str] | None = None,
        persist: bool = True,
    ) -> AsyncGenerator[AgentEvent, None]:
        app = self.app
        current = asyncio.current_task()
        if current is not None:
            app.killswitch.track(current)
        app.killswitch.check()

        history = self.history(conversation_id) if persist else []
        if persist:
            app.conversations.add(conversation_id, "user", user_text, {"source": source})
        messages: list[ChatMessage] = merge_roles(
            [*history, ChatMessage(role="user", content=user_text)]
        )
        ctx = ToolContext(app=app, session_id=conversation_id, source=source, project_id=project_id)
        specs: list[ToolSpec] = app.executor.specs(ctx, names=tool_names) if use_tools else []
        system = await self.system_prompt(
            user_text, source=source, extra=system_extra, with_tools=bool(specs)
        )
        sensitive = app.conversations.is_sensitive(conversation_id) if persist else False
        session = conversation_id
        tools_used: list[dict[str, Any]] = []
        images: list[dict[str, Any]] = []
        shown_text: list[str] = []
        app.events.publish(
            "chat.started", {"source": source, "task": task.value}, session_id=session
        )

        final: ChatResponse | None = None
        # Werkzeuge, die Claude Code selbst über die MCP-Brücke aufruft, laufen am Agenten
        # vorbei – über den Ereignisbus landen sie trotzdem im Gesprächsverlauf.
        bridged = app.events.subscribe(session_id=session, types={"tool.finished"})
        try:
            for step in range(app.config.agent.max_steps + 1):
                last_step = step == app.config.agent.max_steps
                step_tools = [] if last_step else specs
                if last_step:
                    messages.append(ChatMessage(role="user", content=_FINAL_NUDGE))
                response: ChatResponse | None = None
                step_text: list[str] = []
                for attempt in range(2):
                    try:
                        async for chunk in app.router.stream(
                            messages,
                            task=task,
                            system=system,
                            pinned_model=pinned_model,
                            project_id=project_id,
                            tools=step_tools,
                            sensitive=sensitive,
                            session_id=session,
                        ):
                            if chunk.text:
                                step_text.append(chunk.text)
                                app.events.publish(
                                    "chat.delta", {"text": chunk.text}, session_id=session
                                )
                                yield AgentEvent("text", chunk.text)
                            if chunk.response is not None:
                                response = chunk.response
                        break
                    except NoModelAvailableError:
                        # Kein Modell mit Werkzeugen erreichbar (z. B. Claude ohne laufenden
                        # Hintergrunddienst) – dann eben ohne Werkzeuge antworten.
                        if attempt or not step_tools or step_text:
                            raise
                        log.info("Kein Modell mit Werkzeugen verfügbar – antworte ohne Werkzeuge")
                        specs = step_tools = []
                        system = await self.system_prompt(
                            user_text, source=source, extra=system_extra, with_tools=False
                        )
                if response is None:  # pragma: no cover - Router garantiert eine Antwort
                    break
                if step_text:
                    shown_text.append("".join(step_text))
                # Im letzten Schritt gibt es keine Werkzeuge mehr – hält sich das Modell
                # nicht daran, gilt die Antwort trotzdem als final.
                if not response.tool_calls or last_step:
                    final = response
                    break
                messages.append(
                    ChatMessage(
                        role="assistant",
                        content=response.text,
                        tool_calls=tuple(response.tool_calls),
                        provider_data=response.provider_data,
                    )
                )
                for call in response.tool_calls:
                    yield AgentEvent("tool", call.name, data={"arguments": dict(call.arguments)})
                    result: ToolResult = await app.executor.execute(call.name, call.arguments, ctx)
                    tools_used.append(
                        {"name": call.name, "ok": result.ok, "preview": result.text[:200]}
                    )
                    images.extend(img.__dict__ for img in result.images)
                    if result.sensitive and not sensitive:
                        sensitive = True
                        if persist:
                            app.conversations.set_sensitive(conversation_id, True)
                    messages.append(
                        ChatMessage(
                            role="tool",
                            content=result.text if result.ok else f"FEHLER: {result.text}",
                            tool_call_id=call.id,
                            name=call.name,
                        )
                    )
        except RouterError as exc:
            text = str(exc)
            app.events.publish("chat.error", {"error": text}, session_id=session)
            if persist:
                app.conversations.add(conversation_id, "error", text, {"source": source})
            yield AgentEvent("error", text)
            return
        finally:
            bridged.close()

        while not bridged.queue.empty():
            event = bridged.queue.get_nowait()
            if event.data.get("source") != "claude_code":
                continue
            tools_used.append(
                {
                    "name": event.data.get("name"),
                    "ok": event.data.get("ok"),
                    "preview": str(event.data.get("preview", ""))[:200],
                }
            )
            images.extend(event.data.get("images") or [])

        answer = "\n\n".join(t.strip() for t in shown_text if t.strip())
        if final is not None and not answer:
            answer = final.text
        meta = {
            "source": source,
            "model": final.model if final else None,
            "tokens_in": final.input_tokens if final else 0,
            "tokens_out": final.output_tokens if final else 0,
            "cost_usd": final.cost_usd if final else 0.0,
            "subscription": final.subscription if final else False,
            "fallbacks": final.router_fallbacks if final else [],
            "tools": tools_used,
            "images": images,
        }
        if persist and answer:
            app.conversations.add(conversation_id, "assistant", answer, meta)
        app.events.publish("chat.final", {"text": answer, **meta}, session_id=session)
        yield AgentEvent("final", answer, response=final, data=meta)

    async def ask(self, conversation_id: str, user_text: str, **kwargs: Any) -> AgentResult:
        """Bequeme Variante: wartet auf die fertige Antwort."""
        text = ""
        response: ChatResponse | None = None
        tools: list[dict[str, Any]] = []
        images: list[dict[str, Any]] = []
        error: str | None = None
        async for event in self.run(conversation_id, user_text, **kwargs):
            if event.kind == "final":
                text = event.text
                response = event.response
                tools = list(event.data.get("tools", []))
                images = list(event.data.get("images", []))
            elif event.kind == "error":
                error = event.text
        return AgentResult(text=text, response=response, tools=tools, images=images, error=error)
