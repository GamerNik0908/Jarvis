"""Web-API und Dashboard. Läuft nur auf 127.0.0.1 und ist mit einem Token geschützt.

Schutzmaßnahmen gegen Zugriffe fremder Webseiten im Browser:

* Bindung an 127.0.0.1 – aus dem Netzwerk nicht erreichbar,
* Prüfung des Host-Headers (verhindert DNS-Rebinding),
* jeder API-Aufruf braucht das Token (Header ``X-Jarvis-Token`` bzw. ``?t=``),
  das nur die Dashboard-Seite selbst kennt; ohne CORS kann keine fremde Seite es lesen,
* WebSocket prüft zusätzlich die Herkunft (Origin).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets as pysecrets
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from jarvis import __version__
from jarvis.core.config import update_local_config
from jarvis.core.setup_checks import missing_ollama_models, run_checks
from jarvis.core.tasks import spawn
from jarvis.core.timeutil import parse_iso
from jarvis.plugins.base import ToolContext
from jarvis.plugins.system_tools import system_snapshot
from jarvis.router.providers.gemini_provider import GeminiProvider
from jarvis.router.providers.ollama_provider import OllamaProvider

if TYPE_CHECKING:
    from jarvis.core.app import JarvisApp
    from jarvis.core.chat import ChatService
    from jarvis.projects.manager import ProjectManager
    from jarvis.self_improve.manager import ImprovementManager

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
_CACHE_TTL_S = 300
_ALLOWED_SECRETS = {
    "GEMINI_API_KEY",
    "POLLINATIONS_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
}


def load_or_create_token(data_dir: Path) -> str:
    path = data_dir / "api_token"
    if path.is_file():
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token
    token = pysecrets.token_hex(24)
    path.write_text(token, encoding="utf-8")
    return token


class ChatIn(BaseModel):
    text: str
    conversation_id: str | None = None
    model: str | None = None
    task: str | None = None


class ApprovalIn(BaseModel):
    approved: bool


class ProjectIn(BaseModel):
    goal: str
    priority: int = 3
    title: str | None = None


class AnswerIn(BaseModel):
    answer: str


class ImprovementIn(BaseModel):
    request: str


class ReminderIn(BaseModel):
    text: str
    due: str
    repeat: str | None = None


class SecretIn(BaseModel):
    name: str
    value: str


class SettingsIn(BaseModel):
    changes: dict[str, Any]


class KillIn(BaseModel):
    engage: bool


class ListenIn(BaseModel):
    enabled: bool


class SpeakIn(BaseModel):
    text: str


class ToolCallIn(BaseModel):
    name: str
    arguments: dict[str, Any] = {}
    session_id: str | None = None
    source: str = "api"


class _Cache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}

    async def get(self, key: str, loader: Callable[[], Awaitable[Any]]) -> Any:
        hit = self._data.get(key)
        if hit is not None and time.monotonic() - hit[0] < _CACHE_TTL_S:
            return hit[1]
        value = await loader()
        self._data[key] = (time.monotonic(), value)
        return value

    def clear(self) -> None:
        self._data.clear()


def create_web_app(
    app: JarvisApp,
    chat: ChatService,
    *,
    token: str,
    port: int,
    restart: Callable[[], None] | None = None,
    shutdown: Callable[[], None] | None = None,
) -> FastAPI:
    api = FastAPI(
        title="JARVIS", version=__version__, docs_url=None, redoc_url=None, openapi_url=None
    )
    # Nur echte Loopback-Namen – jeder weitere Name (auch "testserver") öffnet DNS-Rebinding.
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    cache = _Cache()

    def token_ok(supplied: str) -> bool:
        # Als Bytes vergleichen: compare_digest wirft bei Nicht-ASCII-Strings TypeError (→ 500).
        return pysecrets.compare_digest(supplied.encode("utf-8"), token.encode("utf-8"))

    @api.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        host = request.headers.get("host", "")
        if host not in allowed_hosts:
            return JSONResponse({"detail": "Host nicht erlaubt"}, status_code=403)
        if request.url.path.startswith("/api/"):
            supplied = request.headers.get("x-jarvis-token") or request.query_params.get("t", "")
            if not token_ok(supplied):
                return JSONResponse({"detail": "Token fehlt oder ist falsch"}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    # ------------------------------------------------------------------ Seiten

    index_template = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @api.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        html = index_template.replace("__JARVIS_TOKEN__", token).replace(
            "__JARVIS_VERSION__", __version__
        )
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    api.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ------------------------------------------------------------------ Status & Übersicht

    def projects_summary() -> list[dict[str, Any]]:
        return app.projects.list() if app.projects is not None else []

    def improvements_summary(limit: int = 8) -> list[dict[str, Any]]:
        return app.improvements.list_jobs(limit=limit) if app.improvements is not None else []

    @api.get("/api/status")
    async def status() -> dict[str, Any]:
        ready = [
            m.id for m in app.router.models() if app.router.model_unavailable_reason(m) is None
        ]
        return {
            "version": __version__,
            "name": app.config.assistant.name,
            "user": app.config.assistant.user_name,
            "killswitch": {"engaged": app.killswitch.engaged, "reason": app.killswitch.reason},
            "models_ready": ready,
            "voice": app.voice.status()
            if app.voice is not None
            else {"enabled": False, "state": "aus"},
            "confirmations": app.broker.pending(),
            "budget": app.costs.status().describe(),
            "hardware": app.hardware.summary() if app.hardware else None,
        }

    async def mail_summary() -> list[dict[str, Any]]:
        google = app.google
        result = []
        for email in google.accounts():
            try:
                unread = await asyncio.to_thread(google.gmail_unread_count, email)
                result.append({"account": email, "unread": unread})
            except Exception as exc:
                result.append({"account": email, "unread": None, "error": str(exc)[:120]})
        return result

    async def calendar_summary() -> list[dict[str, Any]]:
        if not app.google.accounts():
            return []
        try:
            return await asyncio.to_thread(app.google.calendar_events, 7, 8)
        except Exception as exc:
            return [{"error": str(exc)[:160]}]

    @api.get("/api/overview")
    async def overview() -> dict[str, Any]:
        snapshot = await asyncio.to_thread(system_snapshot)
        return {
            "system": snapshot,
            "cards": app.cards.list(),
            "reminders": [r.to_dict() for r in app.reminders.upcoming(8)],
            "mail": await cache.get("mail", mail_summary),
            "calendar": await cache.get("calendar", calendar_summary),
            "projects": projects_summary()[:6],
            "improvements": improvements_summary(5),
            "suggestions": app.improvements.suggestions() if app.improvements else [],
            "images": app.images.list(8),
            "confirmations": app.broker.pending(),
            "costs": app.costs.status().describe(),
        }

    @api.get("/api/system")
    async def system() -> dict[str, Any]:
        return await asyncio.to_thread(system_snapshot)

    async def load_weather() -> dict[str, Any] | None:
        location = app.config.assistant.home_location.strip()
        if not location:
            return None
        from jarvis.plugins.web import WEATHER_CODES, fetch_weather

        try:
            data = await fetch_weather(ToolContext(app=app, source="dashboard"), location)
        except Exception as exc:
            log.info("Wetter für die Übersicht nicht abrufbar: %s", exc)
            return None
        current = data.get("current", {})
        return {
            "place": data.get("place"),
            "current": current,
            "text": WEATHER_CODES.get(int(current.get("weather_code", -1)), ""),
            "daily": data.get("daily", {}),
        }

    @api.get("/api/weather")
    async def weather() -> dict[str, Any]:
        result = await cache.get("weather", load_weather)
        return result or {}

    @api.post("/api/overview/refresh")
    async def overview_refresh() -> dict[str, bool]:
        cache.clear()
        return {"ok": True}

    @api.get("/api/setup")
    async def setup() -> dict[str, Any]:
        # Frisch prüfen: z. B. Ollama oder die Claude-Anmeldung wurden evtl. gerade erst eingerichtet.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(app.startup_probes(), 20)
        return {
            "checks": [c.to_dict() for c in run_checks(app)],
            "missing_models": missing_ollama_models(app),
        }

    @api.post("/api/setup/ollama-pull")
    async def ollama_pull() -> dict[str, Any]:
        ollama = app.provider_of_type(OllamaProvider)
        if ollama is None:
            raise HTTPException(404, "Ollama ist nicht konfiguriert")
        missing = missing_ollama_models(app)

        async def pull_all() -> None:
            for model in missing:
                app.events.publish("setup.pull", {"model": model, "status": "start"})
                try:
                    last = 0.0
                    async for progress in ollama.pull(model):
                        total = progress.get("total") or 0
                        done = progress.get("completed") or 0
                        if time.monotonic() - last > 1 or progress.get("status") == "success":
                            last = time.monotonic()
                            app.events.publish(
                                "setup.pull",
                                {
                                    "model": model,
                                    "status": progress.get("status"),
                                    "percent": round(done * 100 / total) if total else None,
                                },
                            )
                except Exception as exc:
                    app.events.publish("setup.pull", {"model": model, "status": f"Fehler: {exc}"})
            await ollama.probe()
            app.events.publish("setup.pull", {"model": "", "status": "fertig"})

        spawn(pull_all(), name="ollama-pull")
        return {"models": missing}

    # ------------------------------------------------------------------ Google (Gmail & Kalender)

    google_login: dict[str, bool] = {"busy": False}

    @api.get("/api/google")
    async def google_status() -> dict[str, Any]:
        google = app.google
        accounts = google.accounts()
        return {
            "configured": google.configured(),
            "accounts": accounts,
            "calendar_account": app.config.google.calendar_account or (accounts[:1] or [""])[0],
            "client_file": str(google.client_file),
            "login_running": google_login["busy"],
        }

    @api.post("/api/google/client")
    async def google_client(file: UploadFile = File(...)) -> dict[str, bool]:  # noqa: B008 - FastAPI-Muster
        raw = await file.read(256_000)
        try:
            await asyncio.to_thread(app.google.save_client, raw)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        log.info("Google-OAuth-Client gespeichert")
        return {"ok": True}

    @api.post("/api/google/accounts")
    async def google_add() -> dict[str, bool]:
        if not app.google.configured():
            raise HTTPException(409, "Zuerst die OAuth-Client-Datei hochladen")
        if google_login["busy"]:
            raise HTTPException(409, "Eine Anmeldung läuft bereits – bitte im Browser abschließen")
        google_login["busy"] = True

        async def login() -> None:
            try:
                email = await asyncio.to_thread(app.google.add_account)
            except Exception as exc:
                log.warning("Google-Anmeldung fehlgeschlagen: %s", exc)
                app.events.publish("google.error", {"error": str(exc)})
            else:
                cache.clear()
                app.events.publish("google.account_added", {"email": email})
                app.notify("Google verbunden", f"{email} ist jetzt mit JARVIS verbunden.")
            finally:
                google_login["busy"] = False

        spawn(login(), name="google-login")
        return {"started": True}

    @api.delete("/api/google/accounts/{email}")
    async def google_remove(email: str) -> dict[str, bool]:
        removed = await asyncio.to_thread(app.google.remove_account, email)
        if not removed:
            raise HTTPException(404, "Konto nicht verbunden")
        cache.clear()
        return {"removed": True}

    # ------------------------------------------------------------------ Chat

    @api.post("/api/chat")
    async def chat_send(body: ChatIn) -> dict[str, str]:
        if not body.text.strip():
            raise HTTPException(400, "Leere Nachricht")
        try:
            conversation = chat.start(
                body.text,
                conversation_id=body.conversation_id,
                source="web",
                model=body.model,
                task=body.task,
            )
        except ValueError as exc:  # z. B. unbekannte Aufgabenart
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:  # Gespräch beantwortet gerade schon
            raise HTTPException(409, str(exc)) from exc
        return {"conversation_id": conversation}

    @api.post("/api/chat/{conversation_id}/cancel")
    async def chat_cancel(conversation_id: str) -> dict[str, bool]:
        return {"cancelled": chat.cancel(conversation_id)}

    @api.get("/api/conversations")
    async def conversations() -> list[dict[str, Any]]:
        return app.conversations.list(limit=60)

    @api.get("/api/conversations/{conversation_id}")
    async def conversation(conversation_id: str) -> dict[str, Any]:
        if not app.conversations.exists(conversation_id):
            raise HTTPException(404, "Gespräch nicht gefunden")
        return {
            "id": conversation_id,
            "busy": chat.busy(conversation_id),
            "messages": [m.to_dict() for m in app.conversations.messages(conversation_id)],
        }

    @api.delete("/api/conversations/{conversation_id}")
    async def conversation_delete(conversation_id: str) -> dict[str, bool]:
        app.conversations.delete(conversation_id)
        return {"ok": True}

    # ------------------------------------------------------------------ Bestätigungen & Not-Aus

    @api.get("/api/confirmations")
    async def confirmations() -> list[dict[str, Any]]:
        return app.broker.pending()

    @api.post("/api/confirmations/{confirmation_id}")
    async def confirm(confirmation_id: str, body: ApprovalIn) -> dict[str, bool]:
        return {"ok": app.broker.resolve(confirmation_id, body.approved)}

    @api.post("/api/killswitch")
    async def killswitch(body: KillIn) -> dict[str, bool]:
        if body.engage:
            app.killswitch.engage("Dashboard", by="web")
            app.broker.deny_all()
        else:
            app.killswitch.release(by="web")
        return {"engaged": app.killswitch.engaged}

    # ------------------------------------------------------------------ Bilder, Karten, Erinnerungen, Gedächtnis

    @api.get("/api/images")
    async def images() -> list[dict[str, Any]]:
        return app.images.list(200)

    @api.get("/api/images/file/{name}")
    async def image_file(name: str) -> FileResponse:
        path = app.images.path_for(name)
        if path is None:
            raise HTTPException(404, "Bild nicht gefunden")
        return FileResponse(path, headers={"Cache-Control": "max-age=86400"})

    @api.delete("/api/images/{image_id}")
    async def image_delete(image_id: int) -> dict[str, bool]:
        return {"ok": app.images.delete(image_id)}

    @api.get("/api/cards")
    async def cards() -> list[dict[str, Any]]:
        return app.cards.list()

    @api.delete("/api/cards/{card_id}")
    async def card_delete(card_id: int) -> dict[str, bool]:
        return {"ok": app.cards.delete(card_id)}

    @api.get("/api/reminders")
    async def reminders() -> list[dict[str, Any]]:
        return [r.to_dict() for r in app.reminders.upcoming(100)]

    @api.post("/api/reminders")
    async def reminder_create(body: ReminderIn) -> dict[str, Any]:
        try:
            reminder = app.reminders.create(body.text, parse_iso(body.due), body.repeat)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return reminder.to_dict()

    @api.delete("/api/reminders/{reminder_id}")
    async def reminder_delete(reminder_id: int) -> dict[str, bool]:
        return {"ok": app.reminders.delete(reminder_id)}

    @api.get("/api/memory")
    async def memory() -> list[dict[str, Any]]:
        return [m.to_dict() for m in app.memory.all(300)]

    @api.delete("/api/memory/{memory_id}")
    async def memory_delete(memory_id: int) -> dict[str, bool]:
        return {"ok": app.memory.forget(memory_id)}

    # ------------------------------------------------------------------ Projekte

    def projects_or_404() -> ProjectManager:
        if app.projects is None:
            raise HTTPException(503, "Projektmodus nicht aktiv")
        return app.projects

    @api.get("/api/projects")
    async def projects() -> list[dict[str, Any]]:
        return projects_summary()

    @api.post("/api/projects")
    async def project_create(body: ProjectIn) -> dict[str, Any]:
        return await projects_or_404().create(body.goal, title=body.title, priority=body.priority)

    @api.get("/api/projects/{project_id}")
    async def project(project_id: str) -> dict[str, Any]:
        result = projects_or_404().get(project_id)
        if result is None:
            raise HTTPException(404, "Projekt nicht gefunden")
        return dict(result)

    @api.post("/api/projects/{project_id}/{action}")
    async def project_action(project_id: str, action: str) -> dict[str, bool]:
        return {"ok": projects_or_404().control(project_id, action)}

    @api.post("/api/project-questions/{question_id}")
    async def project_answer(question_id: int, body: AnswerIn) -> dict[str, bool]:
        return {"ok": projects_or_404().answer(question_id, body.answer)}

    # ------------------------------------------------------------------ Selbstverbesserung

    def improver() -> ImprovementManager:
        if app.improvements is None:
            raise HTTPException(503, "Selbstverbesserung nicht aktiv")
        return app.improvements

    @api.get("/api/improvements")
    async def improvements() -> dict[str, Any]:
        manager = improver()
        return {
            "jobs": manager.list_jobs(100),
            "suggestions": manager.suggestions(),
            "unavailable": manager.unavailable_reason(),
            "auto_merge": app.config.self_improve.auto_merge,
        }

    @api.post("/api/improvements")
    async def improvement_create(body: ImprovementIn) -> dict[str, Any]:
        if not body.request.strip():
            raise HTTPException(400, "Bitte beschreiben, was verbessert werden soll")
        return improver().request(body.request, source="web")

    @api.get("/api/improvements/{job_id}")
    async def improvement(job_id: int) -> dict[str, Any]:
        manager = improver()
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(404, "Nicht gefunden")
        return dict(job) | {"log": manager.read_log(job_id)}

    @api.get("/api/improvements/{job_id}/diff")
    async def improvement_diff(job_id: int) -> Response:
        text = await improver().diff(job_id)
        return Response(text, media_type="text/plain; charset=utf-8")

    @api.post("/api/improvements/{job_id}/approve")
    async def improvement_approve(job_id: int) -> dict[str, Any]:
        try:
            return await improver().approve(job_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/api/improvements/{job_id}/reject")
    async def improvement_reject(job_id: int) -> dict[str, bool]:
        return {"ok": await improver().reject(job_id)}

    @api.post("/api/improvements/{job_id}/cancel")
    async def improvement_cancel(job_id: int) -> dict[str, bool]:
        return {"ok": await improver().cancel(job_id)}

    @api.post("/api/improvements/{job_id}/retry")
    async def improvement_retry(job_id: int) -> dict[str, Any]:
        manager = improver()
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(404, "Nicht gefunden")
        return manager.request(job["request"], source="retry")

    @api.post("/api/suggestions/{suggestion_id}/accept")
    async def suggestion_accept(suggestion_id: int) -> dict[str, Any]:
        job = improver().implement_suggestion(suggestion_id)
        if job is None:
            raise HTTPException(404, "Vorschlag nicht gefunden")
        return dict(job)

    @api.post("/api/suggestions/{suggestion_id}/dismiss")
    async def suggestion_dismiss(suggestion_id: int) -> dict[str, bool]:
        improver().dismiss_suggestion(suggestion_id)
        return {"ok": True}

    @api.post("/api/improvements-self-check")
    async def self_check() -> dict[str, bool]:
        spawn(improver().self_check(), name="self-check")
        return {"started": True}

    # ------------------------------------------------------------------ Protokolle, Modelle, Kosten

    @api.get("/api/audit")
    async def audit(limit: int = 200) -> list[dict[str, Any]]:
        # Negative Werte hießen bei SQLite "ohne Grenze" – daher nach unten begrenzen.
        return app.audit.recent(min(max(limit, 0), 1000))

    @api.get("/api/logs")
    async def logs(lines: int = 300) -> dict[str, Any]:
        try:
            content = app.log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            content = []
        # content[-0:] wäre die ganze Datei – daher über die Länge schneiden.
        count = min(max(lines, 0), 3000)
        return {"file": str(app.log_file), "lines": content[len(content) - count :]}

    @api.get("/api/models")
    async def models() -> dict[str, Any]:
        return {
            "models": [
                {
                    "id": m.id,
                    "label": m.label,
                    "provider": m.provider,
                    "target": m.target,
                    "local": m.local,
                    "trusted": m.trusted,
                    "subscription": m.subscription,
                    "tools": m.supports_tools,
                    "paid": m.is_paid,
                    "status": app.router.model_unavailable_reason(m) or "bereit",
                }
                for m in app.router.models()
            ],
            "routes": {
                task.value: [t.model for t in targets]
                for task, targets in app.router.config.routes.items()
            },
        }

    @api.get("/api/costs")
    async def costs() -> dict[str, Any]:
        return {
            "status": app.costs.status().describe(),
            "day": [r.__dict__ for r in app.costs.breakdown("day")],
            "month": [r.__dict__ for r in app.costs.breakdown("month")],
        }

    # ------------------------------------------------------------------ Einstellungen & Keys

    @api.get("/api/settings")
    async def settings() -> dict[str, Any]:
        config = app.config
        return {
            "assistant": config.assistant.model_dump(exclude={"system_prompt"}),
            "voice": config.voice.model_dump(),
            "self_improve": config.self_improve.model_dump(),
            "projects": config.projects.model_dump(),
            "images": config.images.model_dump(),
            "budget": config.budget.model_dump(),
            "desktop": config.desktop.model_dump(),
            "config_file": str(app.loaded.path),
            "local_file": str(app.loaded.path.with_name("config.local.yaml")),
            "secrets": {
                name: app.secrets.lookup(name)[1] or None for name in sorted(_ALLOWED_SECRETS)
            },
        }

    @api.post("/api/settings")
    async def settings_update(body: SettingsIn) -> dict[str, Any]:
        try:
            path = update_local_config(app.loaded, body.changes)
        except Exception as exc:
            raise HTTPException(400, f"Ungültige Einstellung: {exc}") from exc
        return {"ok": True, "file": str(path), "restart_needed": True}

    @api.post("/api/secrets")
    async def secret_set(body: SecretIn) -> dict[str, Any]:
        name = body.name.strip().upper()
        if name not in _ALLOWED_SECRETS:
            raise HTTPException(400, "Unbekannter Schlüsselname")
        try:
            app.secrets.set(name, body.value)
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc
        app.router.unblock()
        gemini = app.provider_of_type(GeminiProvider)
        if gemini is not None:
            gemini.refresh()
        return {"ok": True, "source": app.secrets.lookup(name)[1]}

    # ------------------------------------------------------------------ Sprache

    @api.post("/api/voice/transcribe")
    async def transcribe(file: UploadFile = File(...)) -> dict[str, str]:  # noqa: B008 - FastAPI-Muster
        if app.voice is None:
            raise HTTPException(503, "Spracherkennung nicht verfügbar")
        data = await file.read()
        suffix = Path(file.filename or "audio.webm").suffix or ".webm"
        try:
            text = await app.voice.transcribe_upload(data, suffix)
        except Exception as exc:
            raise HTTPException(500, f"Spracherkennung fehlgeschlagen: {exc}") from exc
        return {"text": text}

    @api.post("/api/voice/speak")
    async def speak(body: SpeakIn) -> Response:
        if app.voice is None:
            raise HTTPException(503, "Sprachausgabe nicht verfügbar")
        data, mime = await app.voice.tts.synthesize(body.text[:2000])
        return Response(data, media_type=mime)

    @api.post("/api/voice/listening")
    async def listening(body: ListenIn) -> dict[str, Any]:
        if app.voice is None:
            raise HTTPException(503, "Sprachsteuerung nicht verfügbar")
        app.voice.set_listening(body.enabled)
        return app.voice.status()

    @api.post("/api/voice/ptt")
    async def push_to_talk() -> dict[str, bool]:
        if app.voice is None:
            raise HTTPException(503, "Sprachsteuerung nicht verfügbar")
        app.voice.push_to_talk()
        return {"ok": True}

    # ------------------------------------------------------------------ Werkzeuge (MCP-Brücke)

    @api.get("/api/tools")
    async def tools(names: str = "") -> dict[str, Any]:
        ctx = ToolContext(app=app, source="claude_code")
        wanted = [n for n in names.split(",") if n] or None
        return {
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                    "category": t.category,
                }
                for t in app.executor.available_tools(ctx, names=wanted)
            ]
        }

    @api.post("/api/tools/call")
    async def tool_call(body: ToolCallIn) -> dict[str, Any]:
        ctx = ToolContext(app=app, session_id=body.session_id, source=body.source)
        result = await app.executor.execute(body.name, body.arguments, ctx)
        if result.sensitive and body.session_id and app.conversations.exists(body.session_id):
            app.conversations.set_sensitive(body.session_id, True)
        return {"ok": result.ok, "text": result.text, "images": [i.__dict__ for i in result.images]}

    # ------------------------------------------------------------------ System

    @api.post("/api/system/restart")
    async def system_restart() -> dict[str, bool]:
        if restart is None:
            raise HTTPException(503, "Neustart nur im Dienst möglich")
        asyncio.get_running_loop().call_later(0.5, restart)
        return {"ok": True}

    @api.post("/api/system/shutdown")
    async def system_shutdown() -> dict[str, bool]:
        if shutdown is None:
            raise HTTPException(503, "Beenden nur im Dienst möglich")
        asyncio.get_running_loop().call_later(0.5, shutdown)
        return {"ok": True}

    # ------------------------------------------------------------------ Live-Ereignisse

    @api.websocket("/ws")
    async def websocket(socket: WebSocket) -> None:
        host = socket.headers.get("host", "")
        origin = socket.headers.get("origin", "")
        supplied = socket.query_params.get("t", "")
        allowed_origins = {f"http://{h}" for h in allowed_hosts}
        if (
            host not in allowed_hosts
            or (origin and origin not in allowed_origins)
            or not token_ok(supplied)
        ):
            await socket.close(code=4403)
            return
        await socket.accept()
        subscription = app.events.subscribe()
        try:
            await socket.send_json({"type": "hello", "data": {"version": __version__}})
            while True:
                event = await subscription.get(limit_s=25)
                if event is None:
                    await socket.send_json({"type": "ping", "data": {}})
                    continue
                await socket.send_json(event.to_dict())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            subscription.close()
            with contextlib.suppress(Exception):
                await socket.close()

    return api
