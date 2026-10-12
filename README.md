# JARVIS – persönlicher KI-Assistent für Windows 11

JARVIS läuft lokal auf deinem PC, nutzt mehrere KI-Modelle (Cloud und lokal) und wird
Phase für Phase zu einem Assistenten ausgebaut, der Aufgaben selbstständig erledigt,
tagelang an Projekten arbeitet und sich kontrolliert selbst verbessert.

**Aktueller Stand: Phase 1** – Text-Chat im Terminal mit KI-Router (Claude), Kosten- und
Budgetkontrolle, Hardware-Erkennung und sicherem Umgang mit API-Keys.
Architektur und Phasenplan: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Was Phase 1 kann

- **Chat im Terminal** mit Streaming; Strg+C bricht nur die laufende Antwort ab.
- **KI-Router**: wählt pro Aufgabenart (`chat`, `planning`, `code`, `review`, `simple`,
  `summarize`) das passende Claude-Modell inkl. Denktiefe (`effort`) und weicht bei
  Ausfällen automatisch auf das nächste Modell der Kette aus.
- **Kostenkontrolle**: jeder Aufruf wird mit Tokens und Kosten in SQLite protokolliert;
  harte Tages- und Monatslimits; Warnung ab 80 %.
- **Hardware-Erkennung**: CPU, RAM, GPU, VRAM, freier Speicher – plus Empfehlung, welche
  lokalen Modelle (Ollama, Phase 3) und welches Whisper-Modell (Phase 6) passen.
- **Sicherheit**: Keys im Windows Credential Manager oder in `.env`, nie im Code;
  automatische Schwärzung von Keys in allen Logs.

## Voraussetzungen

| | |
|---|---|
| Betriebssystem | Windows 11 (Windows 10 funktioniert ebenfalls) |
| Python | **3.12 empfohlen** (3.11–3.13 werden unterstützt) |
| Git | zum Klonen und ab Phase 8 für die Selbstverbesserung |
| Anthropic API-Key | unter <https://console.anthropic.com> → *API Keys* |
| optional | aktueller NVIDIA-Treiber (dann wird auch der freie VRAM erkannt) |

## Installation (Windows 11, PowerShell)

```powershell
# 1. Python und Git installieren (falls noch nicht vorhanden)
winget install Python.Python.3.12
winget install Git.Git
# danach ein NEUES PowerShell-Fenster öffnen

# 2. Projekt holen
git clone https://github.com/GamerNik0908/Jarvis.git
cd Jarvis
git checkout claude/jarvis-ai-assistant-7vy3ok   # solange Phase 1 noch nicht in main ist

# 3. Installieren (legt .venv an, installiert Pakete, zeigt deine Hardware)
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1

# 4. API-Key sicher im Windows Credential Manager speichern
#    Einfügen mit Strg+V oder Rechtsklick – es wird nichts angezeigt, dann Enter.
.\scripts\jarvis.cmd secrets set ANTHROPIC_API_KEY

# 5. Prüfen – inkl. einer Mini-Testanfrage (kostet weniger als 0,1 Cent)
.\scripts\jarvis.cmd check --live

# 6. Chat starten
.\scripts\jarvis.cmd
```

> `-ExecutionPolicy Bypass` gilt nur für diesen einen Aufruf und ändert keine
> Systemeinstellung.

<details>
<summary>Manuelle Installation ohne Skript</summary>

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\jarvis.exe check
```

Nach `.\.venv\Scripts\Activate.ps1` steht der Befehl `jarvis` direkt zur Verfügung.
</details>

**Alternative zum Credential Manager:** `.env.example` nach `.env` kopieren und
`ANTHROPIC_API_KEY=...` eintragen. Ein Wert in `.env` hat Vorrang vor dem Credential Manager.

## Bedienung

### Im Chat

| Befehl | Wirkung |
|---|---|
| `/hilfe` | alle Befehle |
| `/neu` | Gesprächsverlauf leeren |
| `/modelle` | Modelle, Preise, Status und Routing |
| `/nutze <id>` / `/nutze auto` | Modell fest wählen (ohne Fallback) / zurück zum Routing |
| `/aufgabe <art>` | Aufgabenart setzen, z. B. `/aufgabe code` → stärkeres Modell, mehr Denktiefe |
| `/kosten` | Kosten heute und diesen Monat, je Modell |
| `/hw` | Hardware-Report und Modell-Empfehlung |
| `/ende` | beenden (auch Strg+D) |

Nach jeder Antwort zeigt eine graue Zeile Modell, Tokens, Dauer, Kosten und Tagesbudget.

### Kommandozeile

`jarvis` steht nach `.\.venv\Scripts\Activate.ps1` zur Verfügung; ohne Aktivierung
funktioniert `.\scripts\jarvis.cmd` mit denselben Argumenten.

```powershell
jarvis                          # Chat (Standard)
jarvis ask "Fasse mir die Idee von asyncio in 3 Sätzen zusammen"
jarvis ask --task code "Schreib eine Python-Funktion, die Primzahlen prüft"
jarvis hw [--json]              # Hardware + Empfehlung
jarvis models                   # Modellkatalog und Routing
jarvis cost                     # Kostenübersicht
jarvis check [--live]           # Konfiguration, Keys, Budget prüfen
jarvis secrets set|delete|status
```

## Konfiguration

Alle Einstellungen stehen kommentiert in [`config.yaml`](config.yaml). Eigene Werte
gehören in eine **`config.local.yaml`** daneben – sie wird nicht committet und
überschreibt nur, was du angibst (Listen werden dabei komplett ersetzt):

```yaml
# config.local.yaml
assistant:
  user_name: Nik
budget:
  daily_limit_usd: 1.00
  monthly_limit_usd: 15.00
router:
  routes:
    chat:            # günstiger chatten: Sonnet zuerst
      - {model: claude-sonnet-5-5, effort: low}
      - {model: claude-haiku-5-5, effort: low}
```

Tippfehler in Schlüsselnamen werden beim Start mit genauer Position gemeldet.

### Wo liegen die Daten?

Standardmäßig unter `%LOCALAPPDATA%\Jarvis` (bewusst getrennt vom Code):

| Datei | Inhalt |
|---|---|
| `jarvis.db` | SQLite: Kosten- und Verbrauchsprotokoll (später Projekte, Aufgaben, Aktionen) |
| `logs\jarvis-chat.log` | Log des Chats (rotierend, Keys geschwärzt) |
| `hardware.json` | letzter Hardware-Report |
| `chat_history.txt` | Eingabeverlauf für die Pfeiltasten (abschaltbar: `ui.save_input_history`) |

Anderer Ort: `paths.data_dir` in der Konfiguration oder Umgebungsvariable `JARVIS_DATA_DIR`.

### Kosten und Budget

- Preise stehen pro Modell in `config.yaml` (USD pro 1 Mio. Tokens, Stand Oktober 2026).
- Ist das Tages- oder Monatslimit erreicht, sperrt der Router alle kostenpflichtigen
  Modelle – das gilt ab Phase 5 auch für Hintergrundprojekte.
- Bricht man eine Antwort ab, berechnet der Anbieter trotzdem die bis dahin erzeugten
  Tokens. JARVIS schätzt diese Kosten konservativ und rechnet sie dem Budget zu.
- Wenn ein Sicherheitsfilter von Claude eine harmlose Anfrage fälschlich ablehnt, lässt
  JARVIS sie serverseitig von einem passenden anderen Claude-Modell beantworten
  (`refusal_fallback: true`). Die Kosten werden pro tatsächlich genutztem Modell berechnet.

## Entwicklung und Tests

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Dev
.\.venv\Scripts\python.exe -m pytest          # Tests (ohne Netzwerk, ohne API-Kosten)
.\.venv\Scripts\python.exe -m ruff check .    # Linting
.\.venv\Scripts\python.exe -m mypy            # Typprüfung (strict)
```

## Fehlerbehebung

| Problem | Lösung |
|---|---|
| „Ausführung von Skripts ist deaktiviert“ | Skript wie oben mit `powershell -ExecutionPolicy Bypass -File …` starten |
| „Kein Python 3.11+ gefunden“ | `winget install Python.Python.3.12`, neues Terminal öffnen |
| „kein API-Key (ANTHROPIC_API_KEY)“ | `jarvis secrets set ANTHROPIC_API_KEY` oder `.env` ausfüllen, JARVIS neu starten |
| Neuer Key im Credential Manager wirkt nicht | Ein (alter) Eintrag in `.env` oder eine Umgebungsvariable hat Vorrang – entfernen |
| „Zugriff verweigert – API-Key prüfen“ | Key in der Anthropic Console prüfen; JARVIS sperrt den Anbieter bis zum Neustart |
| „Abrechnungsproblem (402)“ | Guthaben/Zahlungsdaten in der Anthropic Console prüfen |
| GPU wird ohne „frei“-Wert angezeigt | normal ohne NVIDIA-Treiber/`nvidia-smi`; VRAM kommt dann aus der Registry |
| Umlaute oder Symbole falsch dargestellt | Windows Terminal verwenden (Standard unter Windows 11) |
| „Tagesbudget erreicht“ | Limit in `config.local.yaml` anpassen oder bis morgen warten |

## Roadmap

| Phase | Inhalt | Status |
|---|---|---|
| 1 | Projektstruktur, Konfiguration, Hardware-Erkennung, KI-Router (Claude), Terminal-Chat | ✅ |
| 2 | Agent-Loop, Plugin-System mit Basis-Tools, Berechtigungssystem, Aktionsprotokoll, Not-Aus | geplant |
| 3 | OpenAI, Gemini, Ollama; automatische Modellwahl; anbieterübergreifender Fallback | geplant |
| 4 | Gedächtnis: Kurzzeit, Langzeit (ChromaDB), strukturierte Daten | geplant |
| 5 | Langzeit-Projektmodus, Hintergrundbetrieb, Tagesberichte | geplant |
| 6 | Sprachsteuerung: Wake-Word, Whisper, Sprachausgabe | geplant |
| 7 | Tray-Icon, globaler Hotkey, Chat-Fenster, Web-Dashboard | geplant |
| 8 | Selbstverbesserung mit Git, Sandbox, Tests und Freigabe | geplant |
| 9 | Feinschliff, Autostart, Installer | geplant |

Änderungen: [CHANGELOG.md](CHANGELOG.md)
