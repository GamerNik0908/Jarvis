# Changelog

Alle nennenswerten Änderungen an JARVIS. Format nach [Keep a Changelog](https://keepachangelog.com/de/1.1.0/),
Versionierung nach [SemVer](https://semver.org/lang/de/).

Ab Phase 8 trägt JARVIS hier jede Selbstverbesserung selbst ein (Abschnitt
„Selbstverbesserung“ mit Branch, Testergebnis und Freigabe).

## [0.1.0] – 2026-10-12 · Phase 1

### Hinzugefügt
- Projektstruktur als Python-Paket `jarvis/` mit `core`, `router`, `security`, `ui`
  sowie Platzhaltern für `plugins`, `memory`, `voice`, `projects`, `self_improve`.
- Konfiguration über `config.yaml` + `config.local.yaml`, validiert mit pydantic.
- Hardware-Erkennung (CPU, RAM, GPU/VRAM via `nvidia-smi` und Windows-Registry,
  Speicherplatz) mit Empfehlung lokaler Modelle und Whisper-Größe.
- KI-Router mit Routing-Tabelle je Aufgabenart, Fallback-Ketten, Plugin-Registry für
  Anbieter (Dekorator, Konfigurationsmodule, Entry-Points).
- Anbieter-Plugin Anthropic (Claude Opus 5.5, Sonnet 5.5, Haiku 5.5) mit Streaming,
  `effort`, Prompt-Caching und serverseitigem Refusal-Fallback.
- Kosten- und Verbrauchsprotokoll in SQLite, Tages-/Monatsbudget mit harter Sperre.
- API-Keys über Umgebung/`.env` oder Windows Credential Manager; Schwärzung in Logs.
- Terminal-Chat mit Befehlen, Kostenanzeige und Abbruch per Strg+C.
- CLI: `chat`, `ask`, `hw`, `models`, `cost`, `check`, `secrets`.
- Installationsskript `scripts/install.ps1` und Starter `scripts/jarvis.cmd`.
- Testsuite (pytest) ohne Netzwerkzugriff.
