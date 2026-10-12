"""Kontrollierte Selbstverbesserung (ab Phase 8).

Jede Änderung entsteht in einem eigenen Git-Branch (git worktree), wird in
einer Sandbox mit pytest getestet und erst nach Freigabe übernommen.
Pfade aus ``jarvis.security.PROTECTED_PATHS`` sind tabu.
"""
