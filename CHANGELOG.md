# Changelog

Alle nennenswerten Änderungen an JARVIS. Format nach [Keep a Changelog](https://keepachangelog.com/de/1.1.0/),
Versionierung nach [SemVer](https://semver.org/lang/de/).

Jede Selbstverbesserung trägt JARVIS hier automatisch ein (Abschnitt
„Selbstverbesserungen“ mit Datum, Auftrag, Branch und Testergebnis).

## [1.0.0] – 2026-10-12 · Komplettausbau, komplett kostenlos

### Hinzugefügt
- **Kostenlose KI-Anbieter**: Claude über *Claude Code* mit dem Claude-Pro-Abo (kein API-Key,
  keine Zusatzkosten), Google Gemini (kostenlose Stufe), lokale Modelle über Ollama
  (Modellwahl passend zur erkannten Hardware), optional OpenAI-kompatible Gratis-Dienste.
- **Router** mit Werkzeug-Unterstützung, Datenschutz-Weiche (private Daten nur an Claude-Abo
  oder lokale Modelle), Pausen bei erschöpften Kontingenten und Abo-Limits, Kostenlos-Modus
  (Budget 0 sperrt Bezahlmodelle).
- **Agent-Loop**: planen → Werkzeug wählen → ausführen → prüfen → weiter; Claude Code nutzt
  die JARVIS-Werkzeuge über eine eigene MCP-Brücke.
- **Werkzeuge (Plugins)**: Dateien, Programme, PowerShell, Websuche und Webseiten, Wetter,
  Bilder erstellen (Pollinations) und suchen (Openverse/DuckDuckGo), Gmail mit mehreren
  Konten, Google Kalender, Erinnerungen, Gedächtnis, Dashboard-Karten, Benachrichtigungen,
  Projekte, Selbstverbesserung.
- **Sicherheit**: Berechtigungsstufen (automatisch/Bestätigung/verboten), Bestätigungen per
  Dashboard, Sprache und Terminal, Not-Aus per Tastenkürzel, vollständiges
  Aktionsprotokoll, Pfadrichtlinien (Systemordner und Schlüsseldateien gesperrt), Löschen
  nur in den Papierkorb.
- **Gedächtnis**: Gesprächsverlauf in SQLite, Langzeitgedächtnis mit lokalen Embeddings
  (Ollama) oder Volltextsuche.
- **Projektmodus**: Planung mit Meilensteinen, Hintergrundarbeit über Tage, Neustart-fest,
  Selbstprüfung durch ein zweites Modell, Rückfragen ohne Stillstand, Prioritäten,
  Tageslimit, Tagesbericht.
- **Selbstverbesserung** mit Claude Code: eigener Git-Worktree und Branch je Auftrag,
  Wächter für geschützten Code, automatische Tests mit Nachbesserungsrunden, Diff und
  Freigabe im Dashboard, optional automatische Übernahme kleiner Plugin-Änderungen,
  CHANGELOG-Eintrag, Neustart und automatisches Rollback; regelmäßiger Selbst-Check mit
  Verbesserungsvorschlägen.
- **Sprache**: Wake-Word „Hey Jarvis“ (offline), Whisper lokal, Edge-Stimmen bzw.
  Windows-Stimme, Push-to-Talk, Mikrofon im Dashboard.
- **Oberfläche**: Web-Dashboard (Übersicht, Chat mit Bildern, Projekte, Selbstverbesserung,
  Bilder, Erinnerungen, Gedächtnis, Protokoll, Modelle, Einrichtung inkl. Google-Konten,
  Einstellungen), Chat-Fenster, Tray-Icon, globale Tastenkürzel.
- **Dauerbetrieb**: Hintergrunddienst mit Supervisor (Neustart nach Absturz/Update),
  Autostart bei der Windows-Anmeldung, Desktop-Verknüpfung.
- Geführte Einrichtung (`jarvis setup`) und überarbeitetes Installationsskript; Google-Konten
  lassen sich direkt im Dashboard verbinden (OAuth-Datei hochladen, Konten hinzufügen/trennen).
- **Drucken**: Drucken-Knopf unter jeder Chat-Antwort (nur diese Antwort, ohne Bedienelemente,
  auch „Als PDF speichern“). Diese Funktion hat JARVIS beim Abnahmetest der
  Selbstverbesserung selbst programmiert und getestet.

### Geändert
- Standardbudget 0 $: kostenpflichtige Modelle (Anthropic-API) sind standardmäßig aus.
- `jarvis` ohne Befehl startet jetzt den Hintergrunddienst und öffnet das Dashboard
  (Terminal-Chat: `jarvis chat`).

## [0.1.0] – 2026-10-12 · Phase 1

### Hinzugefügt
- Projektstruktur als Python-Paket `jarvis/` mit `core`, `router`, `security`, `ui`.
- Konfiguration über `config.yaml` + `config.local.yaml`, validiert mit pydantic.
- Hardware-Erkennung (CPU, RAM, GPU/VRAM, Speicherplatz) mit Modell-Empfehlung.
- KI-Router mit Routing-Tabelle je Aufgabenart, Fallback-Ketten und Anbieter-Plugins.
- Kosten- und Verbrauchsprotokoll in SQLite, Tages-/Monatsbudget mit harter Sperre.
- API-Keys über Umgebung/`.env` oder Windows Credential Manager; Schwärzung in Logs.
- Terminal-Chat, CLI, Installationsskript, Testsuite.
