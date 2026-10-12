"""Tool-/Plugin-System (ab Phase 2).

Jede Fähigkeit (Dateien, Shell, Programme, Websuche, E-Mail, Systeminfos, …)
wird ein eigenständiges Plugin mit Beschreibung, Eingabe-Schema und
Berechtigungsstufe. Der Agent-Loop ruft Plugins nur über die
Berechtigungsprüfung in ``jarvis.security`` auf.
"""
