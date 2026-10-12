"""Autostart unter Windows: Eintrag in ``HKCU\\...\\Run`` (keine Admin-Rechte nötig).

Beim Anmelden startet ``pythonw.exe -m jarvis run --background`` – ohne
Konsolenfenster. JARVIS läuft dann im Tray und ist immer aktiv.
Ein Windows-Dienst wäre ungeeignet: Dienste haben keinen Zugriff auf Desktop,
Tray-Icon, Tastenkürzel und (praktisch) das Mikrofon.
"""

from __future__ import annotations

import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "JARVIS"


def command(project_root: Path) -> str:
    executable = Path(sys.executable)
    pythonw = executable.with_name("pythonw.exe")
    if pythonw.is_file():
        executable = pythonw
    return f'"{executable}" -m jarvis run --background'


def is_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
            return True
    except OSError:
        return False


def enable(project_root: Path) -> str:
    if sys.platform != "win32":
        raise RuntimeError("Autostart wird nur unter Windows eingerichtet")
    import winreg

    value = command(project_root)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, value)
    return value


def disable() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
            return True
    except OSError:
        return False
