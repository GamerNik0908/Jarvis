# JARVIS – Architektur

Dieses Dokument richtet sich an Entwickler – und an Claude Code, wenn JARVIS sich selbst
verbessert. Wer etwas ändert, liest es zuerst.

## Leitprinzipien

1. **Kostenlos zuerst.** Claude läuft über Claude Code mit dem Pro-Abo, Gemini über die
   kostenlose Stufe, lokale Modelle über Ollama. Das globale Budget steht auf 0 $ und sperrt
   damit alle kostenpflichtigen Modelle im Router.
2. **Sicherheit sitzt im Kern.** Berechtigungen, Not-Aus und Protokoll greifen im
   `ToolExecutor` – egal ob ein Werkzeug aus dem Chat, per Sprache, aus einem Projekt oder
   von Claude Code (MCP-Brücke) aufgerufen wird.
3. **Code und Daten sind getrennt.** Code im Git-Repository (das JARVIS selbst verändern
   darf), Daten in `%LOCALAPPDATA%\Jarvis` (`JARVIS_DATA_DIR`).
4. **Alles ist ein Plugin**: Werkzeuge (`jarvis/plugins/`), KI-Anbieter
   (`jarvis/router/providers/`), Sprach-Engines (`jarvis/voice/engines.py`).
5. **Geschützter Kern.** `jarvis/security/`, `jarvis/self_improve/`,
   `jarvis/core/supervisor.py`, `.env`, `config.local.yaml` und die config-Abschnitte
   `security`, `budget`, `secrets`, `self_improve` darf die Selbstverbesserung nicht ändern
   (`jarvis/security/guard.py`).

## Prozesse

```text
Windows-Anmeldung ──► pythonw -m jarvis run --background        (Autostart, HKCU\…\Run)
                        │  Supervisor (jarvis/core/supervisor.py)
                        │  · startet den Dienst neu bei Exit-Code 75 (Update) oder Absturz
                        │  · rollt ein Update zurück, wenn der Dienst danach nicht gesund startet
                        ▼
                      python -m jarvis service                   (jarvis/core/service.py)
                        ├─ Web-Server (FastAPI/uvicorn, 127.0.0.1:8765) ◄── Dashboard, Chat-Fenster,
                        │                                                    CLI (jarvis stop/status/improve)
                        ├─ ChatService → Agent → Router → Anbieter
                        ├─ ProjectManager (Hintergrund-Worker)
                        ├─ ImprovementManager (Selbstverbesserung, Selbst-Check)
                        ├─ Scheduler (Erinnerungen, Tagesbericht, Selbst-Check)
                        ├─ VoiceService (Wake-Word, Whisper, TTS – eigener Thread)
                        └─ Desktop (Tray, globale Hotkeys, Benachrichtigungen)

claude -p … (Claude Code) ──► python -m jarvis mcp-bridge ──HTTP──► /api/tools/call
```

`jarvis` ohne Befehl startet den Supervisor im Hintergrund und öffnet das Dashboard.
`jarvis chat` / `jarvis ask` laufen ohne Dienst direkt im Terminal (Claude dann ohne
Werkzeuge, weil die MCP-Brücke den Dienst braucht).

## Ordnerstruktur

```text
jarvis/
├── cli.py                    Befehle: start, run, service, stop, status, window, setup, chat, ask,
│                             improve, google, secrets, autostart, models, cost, hw, check
├── core/
│   ├── app.py                JarvisApp: verdrahtet alles (bootstrap), notify(), system_prompt()
│   ├── config.py             pydantic-Schema, config.yaml + config.local.yaml, update_local_config()
│   ├── agent.py              Agent-Loop: Router-Stream → Werkzeuge → Ergebnis zurück → weiter
│   ├── chat.py               ChatService: ein laufender Agent je Gespräch, Abbruch
│   ├── service.py            Hintergrunddienst (siehe oben)
│   ├── supervisor.py         [geschützt] Neustart + Rollback
│   ├── scheduler.py          minütlicher Takt: Erinnerungen, Tagesbericht, Selbst-Check
│   ├── events.py             EventBus (thread-sicher) → WebSocket /ws, Tray, Sprache
│   ├── db.py                 SQLite (WAL) mit Migrationen
│   ├── stores.py             Erinnerungen, Dashboard-Karten, Bildergalerie
│   ├── setup_checks.py       Einrichtungsstatus (Dashboard „Einrichtung“, jarvis check)
│   ├── hardware.py           CPU/RAM/GPU/VRAM → Empfehlung lokaler Modelle und Whisper-Größe
│   ├── autostart.py          HKCU-Run-Eintrag
│   └── tasks.py, timeutil.py, jsonutil.py, logging_setup.py, types.py
├── router/
│   ├── base.py               ChatMessage, ToolSpec, ToolCall, ChatRequest/Response, LLMProvider
│   ├── router.py             Modellwahl je Aufgabenart, Fallback, Werkzeug-/Datenschutz-Filter,
│   │                         Pausen bei Limits (retry_after_s), Budget, Kostenbuchung
│   ├── costs.py              Verbrauch, Tages-/Monatslimit, Kostenlos-Modus
│   ├── registry.py           Anbieter-Plugins (@register_provider, Entry-Points)
│   └── providers/            claude_code (Pro-Abo) · gemini · ollama · openai_compat · anthropic (API)
├── integrations/
│   ├── claude_code.py        Claude-Code-CLI headless (stream-json), Fehlerklassen, Limit-Reset
│   ├── mcp_bridge.py         minimaler MCP-Server (stdio) → JARVIS-Werkzeuge per HTTP
│   └── google.py             Gmail (mehrere Konten) + Kalender, OAuth pro Konto
├── plugins/                  Werkzeuge (siehe unten), executor.py = Berechtigungen + Protokoll
├── memory/                   store.py (Langzeitgedächtnis: Embeddings oder FTS5), conversations.py
├── projects/manager.py       Projekte: Planung, Aufgaben, Prüfung, Rückfragen, Tagesbericht
├── self_improve/             [geschützt] manager.py, git.py (Worktrees), sandbox.py (Tests)
├── security/                 [geschützt] permissions, audit, killswitch, guard, paths, secrets, redaction
├── voice/                    audio.py, engines.py (WakeWord, SpeechToText, TextToSpeech), service.py
└── ui/
    ├── web/server.py         REST-API + WebSocket, Token- und Host-Prüfung
    ├── web/static/           Dashboard (Vanilla-JS-SPA: index.html, app.js, style.css)
    ├── desktop.py            Tray, Hotkeys, Chat-Fenster (pywebview), Benachrichtigungen
    ├── terminal.py, render.py, setup_wizard.py
```

## Ablauf einer Anfrage

1. Eingabe (Dashboard, Sprache, Terminal, Projekt) → `ChatService.start()` bzw. `Agent.run()`.
2. `Agent` baut den System-Prompt (Datum, Nutzer, passende Erinnerungen aus dem Gedächtnis)
   und die Werkzeugliste (`executor.specs()` – nur erlaubte und verfügbare Werkzeuge).
3. `ModelRouter.stream()` wählt das erste passende Modell der Route
   (`router.routes.<aufgabe>` in config.yaml) und filtert:
   - Werkzeuge nötig → nur `supports_tools`-Modelle, deren Anbieter Werkzeuge gerade kann,
   - Gespräch ist *sensibel* (ein Werkzeug lieferte private Daten) → nur `trusted`-Modelle
     (Claude-Abo, lokal) – nie die kostenlose Gemini-Stufe,
   - pausierte Modelle (Kontingent erschöpft) und gesperrte Anbieter (Auth-Fehler),
   - Kosten > 0 bei Budget 0.
   Fällt ein Modell aus, bevor Text kam, übernimmt das nächste.
   Gibt es kein Modell mit Werkzeugen, antwortet der Agent ohne Werkzeuge.
4. Liefert das Modell Werkzeug-Aufrufe, führt `ToolExecutor.execute()` sie aus
   (Berechtigung → ggf. Bestätigung → Not-Aus-Prüfung → Ausführung mit Timeout →
   Protokoll → Ereignisse `tool.started/finished`, `image.new`) und gibt das Ergebnis zurück.
5. Antwort, Werkzeuge und Bilder landen im Gesprächsverlauf (`ConversationStore`);
   Ereignisse `chat.delta/final/error` gehen live ans Dashboard.

**Claude Code als Anbieter:** Im Chat-Modus läuft `claude -p` ohne eigene Werkzeuge
(`--tools ""`), im Agent-Modus mit `--mcp-config` auf die JARVIS-MCP-Brücke und
`--permission-mode dontAsk` (nur `mcp__jarvis` erlaubt). Claude ruft die JARVIS-Werkzeuge
dann selbst auf – Berechtigungen, Bestätigungen und Protokoll laufen trotzdem durch den
`ToolExecutor`. `ANTHROPIC_API_KEY` wird aus der Umgebung entfernt, damit immer das Abo
genutzt wird; `--bare` darf nicht verwendet werden (das erzwingt API-Keys).

## Werkzeuge schreiben

Ein Plugin ist ein Modul in `jarvis/plugins/` – es wird automatisch geladen:

```python
from typing import Any

from jarvis.plugins.base import ToolContext, ToolResult, obj, tool
from jarvis.security.permissions import Level


@tool(
    name="print_text",                       # eindeutiger Name (englisch, snake_case)
    description="Print a text on the default Windows printer.",   # fürs Modell, englisch
    parameters=obj({"text": {"type": "string"}}, required=["text"]),
    level=Level.CONFIRM,                     # AUTO | CONFIRM | FORBIDDEN oder Funktion(args, ctx)
    category="system",
    summary=lambda args: f"Drucken: {args['text'][:60]}",   # Text im Bestätigungsdialog
)
async def print_text(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    ...
    return ToolResult("Gedruckt.")            # ok=False / ToolResult.error(...) bei Fehlern
```

- Blockierendes (Dateien, Windows-APIs, `requests`) mit `asyncio.to_thread` auslagern.
- HTTP über `ctx.app.http()` (gemeinsamer httpx-Client mit User-Agent).
- Private Daten zurückgeben → `ToolResult(..., sensitive=True)`.
- Bilder → `ctx.app.images.save(...)` und `images=[ImageRef(url=..., caption=..., path=...)]`.
- Dateipfade immer über `ctx.app.paths` (PathPolicy) prüfen bzw. deren Level nutzen.
- Windows-spezifisches mit `sys.platform == "win32"` absichern; Tests laufen auch unter Linux.
- Pro Werkzeug Tests in `tests/` (siehe `tests/test_tools_basic.py`).

## Anbieter schreiben

`LLMProvider` aus `jarvis/router/base.py` ableiten, mit `@register_provider` registrieren,
`stream()` implementieren (Text-Chunks, am Ende genau ein `StreamChunk(response=...)` mit
`ChatResponse`), Fehler als `ProviderAuthError` / `ProviderUnavailableError(retry_after_s=…)` /
`ProviderRequestError` melden. Modelle und Routen kommen in `config.yaml`.

## Dashboard

`jarvis/ui/web/server.py` stellt die API bereit (alle Pfade unter `/api/…`, Token im Header
`X-Jarvis-Token` oder `?t=`; Host muss `127.0.0.1`/`localhost` sein). Live-Ereignisse kommen
über `/ws`. Das Frontend ist eine Vanilla-JS-SPA ohne Build-Schritt
(`jarvis/ui/web/static/app.js`): Seiten sind `render<Name>(view)`-Funktionen, registriert in
der `pages`-Tabelle; DOM-Aufbau mit dem Helfer `h(tag, attrs, ...children)`.

## Selbstverbesserung

```text
Wunsch ─► ImprovementManager.request()
          ├─ git worktree add <data>/worktrees/<id>  (Branch jarvis/verbesserung-<id>-<slug>)
          ├─ Claude Code im Worktree (acceptEdits, erlaubte Werkzeuge, geschützte Pfade gesperrt)
          ├─ Guard: geschützte Dateien/config-Abschnitte geändert? → verwerfen
          ├─ pytest im Worktree (PYTHONPATH = Worktree; eigenes venv nur bei neuen Paketen)
          │    └─ rot → Nachbesserung per --resume (max_fix_rounds)
          ├─ Status „bereit“: Zusammenfassung + Diff im Dashboard
          └─ Freigabe ─► merge --no-ff ─► CHANGELOG ─► update_pending.json ─► Neustart (Exit 75)
                          Supervisor: neuer Dienst nicht gesund (service_healthy fehlt) → git reset
                          auf den alten Stand + Hinweis im Dashboard
```

Der Selbst-Check (`self_check`) liest nur: Claude Code analysiert Code und protokollierte
Fehler der letzten 7 Tage und legt Vorschläge an, die der Benutzer mit „Umsetzen“ startet.

## Daten (`%LOCALAPPDATA%\Jarvis`)

| Pfad | Inhalt |
|---|---|
| `jarvis.db` | SQLite: Verbrauch, Gespräche, Gedächtnis (+FTS5), Erinnerungen, Karten, Bilder, Projekte, Aufgaben, Rückfragen, Aktionsprotokoll, Verbesserungen, Zustände |
| `logs/` | rotierende Logs je Rolle (`jarvis-service.log`, …), Keys geschwärzt |
| `images/` | erstellte und gespeicherte Bilder |
| `google/credentials.json`, `google/tokens/` | OAuth-Client und Zugänge pro Gmail-Konto |
| `worktrees/`, `improvements/` | Arbeitskopien und Logs der Selbstverbesserung |
| `api_token` | Zugriffsschlüssel für Dashboard/API |
| `hardware.json` | Hardware-Report (24 h zwischengespeichert) |

## Tests und Qualität

- `python -m pytest -q` – ohne Netzwerk und ohne Kosten; Anbieter werden mit
  `httpx.MockTransport` bzw. dem `FakeProvider` aus `tests/conftest.py` simuliert.
- `python -m ruff check .` und `python -m ruff format .` (Zeilenlänge 100).
- `python -m mypy` (strict) für das Paket `jarvis`.
