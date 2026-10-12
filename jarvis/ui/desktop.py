"""Desktop-Integration: Tray-Icon, globale Tastenkürzel, Windows-Benachrichtigungen, Chat-Fenster.

Alles hier ist optional: Fehlen die Pakete (``pystray``, ``pynput``, ``pywebview``)
oder läuft JARVIS ohne Desktop, startet der Dienst trotzdem.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
import sys
import threading
import webbrowser
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jarvis.core.app import JarvisApp

log = logging.getLogger(__name__)


def tray_image(size: int = 64, *, alert: bool = False) -> Any:
    """Zeichnet das JARVIS-Symbol (Arc-Reactor) mit Pillow."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    ring = (239, 68, 68, 255) if alert else (34, 211, 238, 255)
    draw.ellipse((2, 2, size - 2, size - 2), fill=(11, 18, 32, 255))
    draw.ellipse((8, 8, size - 8, size - 8), outline=ring, width=max(3, size // 14))
    core = size // 4
    draw.ellipse(
        (
            size // 2 - core // 2 - 4,
            size // 2 - core // 2 - 4,
            size // 2 + core // 2 + 4,
            size // 2 + core // 2 + 4,
        ),
        fill=ring,
    )
    draw.ellipse(
        (
            size // 2 - core // 4,
            size // 2 - core // 4,
            size // 2 + core // 4,
            size // 2 + core // 4,
        ),
        fill=(224, 251, 255, 255),
    )
    return image


def open_window(url: str) -> None:
    """Öffnet das Chat-Fenster als eigenen Prozess (pywebview), sonst den Browser."""
    try:
        import webview  # noqa: F401
    except ImportError:
        webbrowser.open(url)
        return
    executable = sys.executable
    if sys.platform == "win32" and executable.lower().endswith("python.exe"):
        candidate = executable[:-10] + "pythonw.exe"
        executable = candidate
    subprocess.Popen(
        [executable, "-m", "jarvis", "window", "--url", url],
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        close_fds=True,
    )


def run_window(url: str) -> int:
    """Wird im Fensterprozess ausgeführt."""
    try:
        import webview
    except ImportError:
        webbrowser.open(url)
        return 0
    webview.create_window(
        "JARVIS", url, width=1320, height=880, min_size=(720, 520), background_color="#070b14"
    )
    webview.start()
    return 0


class Desktop:
    def __init__(
        self,
        app: JarvisApp,
        *,
        url: str,
        restart: Callable[[], None],
        shutdown: Callable[[], None],
    ) -> None:
        self.app = app
        self.url = url
        self._restart = restart
        self._shutdown = shutdown
        self._icon: Any = None
        self._hotkeys: Any = None

    def start(self) -> None:
        config = self.app.config.desktop
        if config.tray:
            threading.Thread(target=self._run_tray, name="jarvis-tray", daemon=True).start()
        if config.hotkeys:
            self._start_hotkeys()
        self.app.notify_handlers.append(self.notify)
        self.app.killswitch.on_engage(self._on_kill)

    def stop(self) -> None:
        if self._icon is not None:
            with contextlib.suppress(Exception):
                self._icon.stop()
        if self._hotkeys is not None:
            with contextlib.suppress(Exception):
                self._hotkeys.stop()

    def open_dashboard(self) -> None:
        open_window(self.url)

    def notify(self, title: str, message: str) -> None:
        if self._icon is not None:
            try:
                self._icon.notify(message[:250], title[:60])
            except Exception:
                log.debug("Tray-Benachrichtigung fehlgeschlagen", exc_info=True)

    def _on_kill(self) -> None:
        if self._icon is not None:
            with contextlib.suppress(Exception):
                self._icon.icon = tray_image(alert=True)

    # ------------------------------------------------------------------ Tray

    def _run_tray(self) -> None:
        try:
            import pystray
        except ImportError:
            log.info("pystray nicht installiert – kein Tray-Icon")
            return
        app = self.app

        def listening_checked(item: Any) -> bool:
            return bool(app.voice is not None and app.voice.listening)

        def toggle_listening(icon: Any, item: Any) -> None:
            if app.voice is not None:
                app.voice.set_listening(not app.voice.listening)

        def push_to_talk(icon: Any, item: Any) -> None:
            if app.voice is not None:
                app.voice.push_to_talk()

        def kill(icon: Any, item: Any) -> None:
            app.killswitch.engage("Tray-Menü", by="tray")
            app.broker.deny_all()

        def release(icon: Any, item: Any) -> None:
            app.killswitch.release(by="tray")
            icon.icon = tray_image()

        menu = pystray.Menu(
            pystray.MenuItem(
                "JARVIS öffnen", lambda icon, item: self.open_dashboard(), default=True
            ),
            pystray.MenuItem("Im Browser öffnen", lambda icon, item: webbrowser.open(self.url)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("„Hey Jarvis“ zuhören", toggle_listening, checked=listening_checked),
            pystray.MenuItem("Jetzt sprechen", push_to_talk),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("NOT-AUS", kill),
            pystray.MenuItem(
                "Not-Aus freigeben", release, visible=lambda item: app.killswitch.engaged
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Neu starten", lambda icon, item: self._restart()),
            pystray.MenuItem("Beenden", lambda icon, item: self._shutdown()),
        )
        try:
            self._icon = pystray.Icon("jarvis", tray_image(), "JARVIS", menu)
            self._icon.run()
        except Exception:
            log.exception("Tray-Icon konnte nicht gestartet werden")

    # ------------------------------------------------------------------ Hotkeys

    def _start_hotkeys(self) -> None:
        try:
            from pynput import keyboard
        except Exception:
            log.info("pynput nicht verfügbar – keine globalen Tastenkürzel")
            return
        app = self.app
        keys = app.config.hotkeys

        def kill() -> None:
            app.killswitch.engage("Tastenkürzel", by="hotkey")
            app.broker.deny_all()

        def talk() -> None:
            if app.voice is not None:
                app.voice.push_to_talk()

        mapping = {
            keys.open_window: self.open_dashboard,
            keys.push_to_talk: talk,
            keys.killswitch: kill,
        }
        try:
            self._hotkeys = keyboard.GlobalHotKeys(mapping)
            self._hotkeys.daemon = True
            self._hotkeys.start()
        except Exception:
            log.exception("Globale Tastenkürzel konnten nicht registriert werden")
