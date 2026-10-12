"""Sicherheitskern von JARVIS.

Hier liegt alles, was Schutzfunktionen betrifft: Umgang mit API-Keys,
Schwärzen von Secrets in Logs und (ab Phase 2) Berechtigungsstufen,
Aktionsprotokoll und Not-Aus.

Dieses Paket und alle Pfade in :data:`PROTECTED_PATHS` darf das
Selbstverbesserungs-Modul (Phase 8) niemals verändern. Die Durchsetzung
erfolgt dort mehrstufig: Diff-Prüfung vor jedem Merge, Prüfsummen beim
Start und schreibgeschützte Dateirechte – eine Python-Konstante allein
wäre kein ausreichender Schutz.
"""

#: Relativ zum Projektordner; Ordner enden mit "/".
PROTECTED_PATHS: tuple[str, ...] = (
    "jarvis/security/",
    "config.yaml",
    "config.local.yaml",
    ".env",
)
