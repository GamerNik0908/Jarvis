"""Der JARVIS-Hintergrunddienst: alles, was dauerhaft läuft.

* Web-Dashboard + API (127.0.0.1)
* Projekt-Worker, Selbstverbesserung, Zeitplaner (Erinnerungen, Berichte, Selbst-Check)
* Sprachsteuerung, Tray-Icon, globale Tastenkürzel

Gestartet wird der Dienst vom Supervisor (``jarvis run``), der ihn bei Updates
neu startet und bei fehlerhaften Updates zurückrollt.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
from collections.abc import Generator
from typing import Any

import uvicorn

from jarvis.core.app import JarvisApp
from jarvis.core.chat import ChatService
from jarvis.core.scheduler import Scheduler
from jarvis.core.supervisor import HEALTHY_MARKER, RESTART_EXIT_CODE
from jarvis.core.tasks import spawn
from jarvis.integrations.mcp_bridge import ToolBridge
from jarvis.projects.manager import ProjectManager
from jarvis.router.providers.claude_code_provider import ClaudeCodeProvider
from jarvis.self_improve.manager import UPDATE_MARKER, ImprovementManager
from jarvis.self_improve.sandbox import base_python
from jarvis.ui.desktop import Desktop, open_window
from jarvis.ui.web.server import create_web_app, load_or_create_token

log = logging.getLogger(__name__)

_HEALTHY_AFTER_S = 20


class _EmbeddedServer(uvicorn.Server):
    """uvicorn ohne eigene Signal-Handler – Beenden steuert der Dienst selbst."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


class JarvisService:
    def __init__(self, app: JarvisApp, *, background: bool = False) -> None:
        self.app = app
        self.background = background
        self.exit_code = 0
        self._stop: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.app.config.web.port}/"

    def request_restart(self) -> None:
        self._finish(RESTART_EXIT_CODE)

    def request_shutdown(self) -> None:
        self._finish(0)

    def _finish(self, code: int) -> None:
        self.exit_code = code
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)

    async def run(self) -> int:
        app = self.app
        config = app.config
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._stop = asyncio.Event()
        app.events.bind_loop(loop)
        app.killswitch.bind_loop(loop)
        if port_in_use(config.web.host, config.web.port):
            log.error("Port %s ist belegt – läuft JARVIS bereits?", config.web.port)
            return 0

        token = load_or_create_token(app.data_dir)
        bridge = ToolBridge(
            api_base=f"http://127.0.0.1:{config.web.port}", token=token, python=base_python()
        )
        for provider in app.router.providers.values():
            if isinstance(provider, ClaudeCodeProvider):
                provider.configure(workdir=app.data_dir / "claude", bridge=bridge)

        chat = ChatService(app)
        scheduler = Scheduler(app)
        if config.projects.enabled:
            app.projects = ProjectManager(app)
            app.projects.start()
        if config.self_improve.enabled:
            app.improvements = ImprovementManager(app, restart=self.request_restart)
            app.improvements.start()
        try:
            from jarvis.voice.service import VoiceService

            app.voice = VoiceService(app)
            app.voice.start(loop)
            app.killswitch.on_engage(app.voice.stop_speaking)
        except Exception as exc:
            log.warning("Sprachsteuerung nicht verfügbar: %s", exc)
            app.voice = None
        scheduler.start()
        probes = asyncio.create_task(app.startup_probes(), name="startup-probes")

        web = create_web_app(
            app,
            chat,
            token=token,
            port=config.web.port,
            restart=self.request_restart,
            shutdown=self.request_shutdown,
        )
        server = _EmbeddedServer(
            uvicorn.Config(
                web,
                host=config.web.host,
                port=config.web.port,
                log_level="warning",
                lifespan="off",
                access_log=False,
                ws="auto",
            )
        )
        server_task = asyncio.create_task(server.serve(), name="web")

        desktop = Desktop(
            app, url=self.url, restart=self.request_restart, shutdown=self.request_shutdown
        )
        try:
            desktop.start()
        except Exception:
            log.exception("Desktop-Integration fehlgeschlagen")

        for _ in range(50):
            if server.started or server_task.done():
                break
            await asyncio.sleep(0.1)
        if server_task.done():
            log.error("Web-Server konnte nicht starten")
            self.exit_code = 1
        else:
            log.info("JARVIS läuft: %s", self.url)
            self._announce_update_result()
            if config.web.open_on_start and not self.background:
                open_window(self.url)
            spawn(self._mark_healthy(), name="healthy")
            try:
                await self._stop.wait()
            except asyncio.CancelledError:
                # Strg+C im Konsolenfenster: geordnet herunterfahren statt hart abbrechen.
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()

        log.info("JARVIS wird beendet (Code %s)", self.exit_code)
        desktop.stop()
        if app.voice is not None:
            app.voice.stop()
        await scheduler.stop()
        if app.projects is not None:
            await app.projects.stop()
        if app.improvements is not None:
            await app.improvements.stop()
        probes.cancel()
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(server_task, 10)
        return self.exit_code

    async def _mark_healthy(self) -> None:
        await asyncio.sleep(_HEALTHY_AFTER_S)
        data_dir = self.app.data_dir
        (data_dir / HEALTHY_MARKER).write_text("ok", encoding="utf-8")
        marker_path = data_dir / UPDATE_MARKER
        if marker_path.is_file():
            marker_path.unlink(missing_ok=True)
            log.info("Update erfolgreich gestartet – Rollback-Markierung entfernt")

    def _announce_update_result(self) -> None:
        data_dir = self.app.data_dir
        notice = data_dir / "rollback_notice.json"
        if notice.is_file():
            try:
                data: dict[str, Any] = json.loads(notice.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
            notice.unlink(missing_ok=True)
            self.app.notify(
                "Update zurückgerollt",
                f"Verbesserung #{data.get('job_id')} hat JARVIS nicht sauber starten lassen und wurde "
                f"automatisch zurückgenommen ({data.get('reason', '')}).",
                level="warning",
            )
        elif (data_dir / UPDATE_MARKER).is_file():
            self.app.notify("Update aktiv", "Die neue Version von JARVIS läuft.")
