"""Sicherheitskern von JARVIS.

* ``secrets`` – API-Keys aus ``.env`` oder dem Windows Credential Manager
* ``redaction`` – Secrets werden in allen Logs geschwärzt
* ``permissions`` – Stufen auto / confirm / forbidden und Bestätigungen
* ``audit`` – vollständiges Aktionsprotokoll
* ``killswitch`` – Not-Aus
* ``guard`` – Schutzregeln für die Selbstverbesserung

Dieses Paket darf die Selbstverbesserung niemals verändern (siehe
``guard.PROTECTED_PATHS``). Die Prüfung erfolgt beim Erzeugen des Diffs und
vor dem Übernehmen, zusätzlich verbietet JARVIS Claude Code das Bearbeiten
dieser Dateien schon beim Start.
"""
