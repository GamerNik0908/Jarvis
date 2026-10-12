"""Langzeit-Projektmodus (ab Phase 5).

Ziel → Plan mit Meilensteinen → Aufgaben-Warteschlange in SQLite. Der Zustand
wird nach jedem Schritt gespeichert, damit JARVIS nach Neustart oder Absturz
genau dort weitermacht. Budget pro Projekt über ``CostTracker.spent_for_project``.
"""
