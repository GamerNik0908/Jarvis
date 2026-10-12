"""Sprachsteuerung: "Hey Jarvis" → zuhören → verstehen → antworten → vorlesen.

Läuft in einem eigenen Thread, der dauerhaft das Mikrofon liest (lokal, nichts
wird ins Internet gesendet, bis das Wake-Word erkannt wurde – und auch dann
bleibt die Spracherkennung lokal). Zusätzlich:

* Push-to-Talk per Hotkey (Standard Strg+Alt+Leertaste) oder Tray-Menü,
* Mikrofon-Knopf im Dashboard (Aufnahme im Browser, Erkennung lokal),
* Sprachbefehle: "Not-Aus", "Stopp", und "Ja"/"Nein" für offene Bestätigungen.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import queue
import re
import threading
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import numpy as np

from jarvis.voice.audio import FRAME, SAMPLE_RATE, EndOfSpeech, beep, capture
from jarvis.voice.engines import SpeechToText, TextToSpeech, WakeWord

if TYPE_CHECKING:
    from jarvis.core.app import JarvisApp

log = logging.getLogger(__name__)

VOICE_CONVERSATION_KEY = "voice_conversation"
_YES = re.compile(
    r"^\s*(ja|jawohl|ok(ay)?|bestätig\w*|mach( das)?|einverstanden|genehmigt)\b", re.I
)
_NO = re.compile(r"^\s*(nein|nee|abbrechen|ablehnen|stopp?)\b", re.I)
_KILL = re.compile(r"\bnot[\s-]?aus\b|\bnotstopp\b|\balles stoppen\b", re.I)
_HUSH = re.compile(r"^\s*(stopp?|sei still|ruhe|halt)\s*[.!]?\s*$", re.I)


class VoiceService:
    def __init__(self, app: JarvisApp) -> None:
        self.app = app
        config = app.config.voice
        recommendation = app.recommendation
        model = config.stt_model
        compute = "int8"
        if model == "auto":
            model = recommendation.whisper_model if recommendation else "small"
            compute = recommendation.whisper_compute_type if recommendation else "int8"
        device = (
            config.stt_device
            if config.stt_device != "auto"
            else (recommendation.whisper_device if recommendation else "cpu")
        )
        self.stt = SpeechToText(
            model, device=device, compute_type=compute, language=config.language
        )
        self.tts = TextToSpeech(config.tts_engine, config.tts_voice)
        self.wake = WakeWord(config.wake_word, config.wake_threshold)
        self.state = "aus"
        self.error: str | None = None
        self.listening = config.enabled
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._push_to_talk = threading.Event()
        self._busy = threading.Event()
        self._speak_queue: queue.Queue[str] = queue.Queue()

    # ------------------------------------------------------------------ Steuerung

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.app.config.voice.enabled,
            "listening": self.listening,
            "state": self.state,
            "error": self.error,
            "wake_word": self.app.config.voice.wake_word,
            "stt_model": self.stt.model_name,
            "tts": self.app.config.voice.tts_engine,
        }

    def _set_state(self, state: str) -> None:
        self.state = state
        self.app.events.publish("voice.state", {"state": state, "listening": self.listening})

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        threading.Thread(target=self._speaker_loop, name="jarvis-tts", daemon=True).start()
        if not self.app.config.voice.enabled:
            self._set_state("aus")
            return
        self._thread = threading.Thread(target=self._mic_loop, name="jarvis-mikrofon", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.tts.stop()

    def set_listening(self, enabled: bool) -> None:
        self.listening = enabled
        self._set_state("bereit" if enabled else "pausiert")

    def push_to_talk(self) -> None:
        """Startet sofort eine Aufnahme (ohne Wake-Word)."""
        self.tts.stop()
        self._push_to_talk.set()

    def say_soon(self, text: str) -> None:
        """Liest einen Text auf den PC-Lautsprechern vor (threadsicher)."""
        if self.app.config.voice.tts_engine != "off" and text.strip():
            self._speak_queue.put(text)

    def stop_speaking(self) -> None:
        with self._speak_queue.mutex:
            self._speak_queue.queue.clear()
        self.tts.stop()

    # ------------------------------------------------------------------ Mikrofon

    def _frames(self) -> Iterator[np.ndarray]:
        import sounddevice as sd

        blocks: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)

        def callback(indata: np.ndarray, frames: int, timing: Any, status: Any) -> None:
            with contextlib.suppress(queue.Full):
                blocks.put_nowait(indata[:, 0].copy())

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=FRAME,
            device=self.app.config.voice.input_device,
            callback=callback,
        ):
            while not self._stop.is_set():
                try:
                    yield blocks.get(timeout=0.5)
                except queue.Empty:
                    continue

    def _mic_loop(self) -> None:
        try:
            self._set_state("lädt")
            self.wake.load()
        except Exception as exc:
            self.error = f"Wake-Word nicht verfügbar: {exc}"
            log.warning(self.error)
        delay = 15.0
        last_error = ""
        while not self._stop.is_set():
            try:
                self._listen()
                delay = 15.0
            except ImportError as exc:
                # Fehlende Pakete tauchen ohne Neustart nicht auf – nicht endlos neu versuchen.
                self.error = f"Sprachpakete fehlen ({exc.name or exc}) – Installationsskript erneut ausführen"
                log.warning("Sprachsteuerung aus: %s", self.error)
                self._set_state("fehler")
                return
            except Exception as exc:
                self.error = f"Mikrofon: {exc}"
                if str(exc) != last_error:
                    log.warning("Sprachsteuerung: %s – neuer Versuch folgt", exc)
                    last_error = str(exc)
                self._set_state("fehler")
                self._stop.wait(delay)
                delay = min(delay * 2, 300.0)

    def _listen(self) -> None:
        frames = self._frames()
        self._set_state("bereit" if self.listening else "pausiert")
        self.error = None if self.error and self.error.startswith("Mikrofon") else self.error
        follow_up_until = 0.0
        for frame in frames:
            if self._stop.is_set():
                return
            if self._busy.is_set() or self.tts.playing:
                continue
            triggered = self._push_to_talk.is_set()
            if not triggered and self.listening and self.wake._model is not None:
                triggered = self.wake.detect(frame)
            in_follow_up = time.monotonic() < follow_up_until
            if not triggered and not in_follow_up:
                continue
            self._push_to_talk.clear()
            if triggered:
                self._play_tone()
            self._set_state("hört zu")
            config = self.app.config.voice
            detector = EndOfSpeech(
                max_s=config.max_record_seconds,
                start_timeout_s=config.follow_up_seconds if in_follow_up and not triggered else 6.0,
            )
            audio = capture(frames, detector, self._stop.is_set)
            follow_up_until = 0.0
            if audio is None or audio.size < SAMPLE_RATE // 3:
                self._set_state("bereit" if self.listening else "pausiert")
                continue
            self._busy.set()
            self._set_state("denkt nach")
            future = asyncio.run_coroutine_threadsafe(self.handle_audio(audio), self._loop)  # type: ignore[arg-type]
            try:
                answered = future.result(timeout=600)
            except Exception:
                log.exception("Sprachverarbeitung fehlgeschlagen")
                answered = False
            finally:
                self._busy.clear()
            while self.tts.playing or not self._speak_queue.empty():
                time.sleep(0.1)
            if answered and config.follow_up_seconds > 0:
                follow_up_until = time.monotonic() + config.follow_up_seconds
            self._set_state("bereit" if self.listening else "pausiert")

    def _play_tone(self) -> None:
        try:
            import sounddevice as sd

            sd.play(beep(), 24_000)
        except Exception:
            pass

    # ------------------------------------------------------------------ Verarbeitung

    async def transcribe_array(self, audio: np.ndarray) -> str:
        return await asyncio.to_thread(self.stt.transcribe, audio)

    async def transcribe_upload(self, data: bytes, suffix: str) -> str:
        return await asyncio.to_thread(self.stt.transcribe_bytes, data, suffix)

    async def handle_audio(self, audio: np.ndarray) -> bool:
        text = await self.transcribe_array(audio)
        if not text:
            return False
        self.app.events.publish("voice.heard", {"text": text})
        reply = await self.handle_text(text)
        if reply:
            self.say_soon(reply)
        return bool(reply)

    async def handle_text(self, text: str) -> str:
        """Verarbeitet einen gesprochenen Satz und liefert die Antwort zum Vorlesen."""
        app = self.app
        if _KILL.search(text):
            app.killswitch.engage("Sprachbefehl", by="voice")
            return "Not-Aus aktiviert. Alle Aufgaben sind gestoppt."
        if _HUSH.match(text):
            self.stop_speaking()
            return ""
        pending = app.broker.latest()
        if pending is not None:
            if _YES.match(text):
                app.broker.resolve(pending["id"], True)
                return "Bestätigt."
            if _NO.match(text):
                app.broker.resolve(pending["id"], False)
                return "Abgelehnt."
        conversation = app.db.get_state(VOICE_CONVERSATION_KEY)
        conversation = app.conversations.ensure(conversation, channel="voice")
        app.db.set_state(VOICE_CONVERSATION_KEY, conversation)
        result = await app.agent.ask(conversation, text, source="voice")
        if result.error:
            return f"Das hat leider nicht geklappt: {result.error[:200]}"
        return result.text

    def _speaker_loop(self) -> None:
        while True:
            text = self._speak_queue.get()
            if self._loop is None:
                continue
            spoken = re.sub(r"[*_`#>|]", "", text)
            spoken = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", spoken)
            try:
                self._set_state("spricht")
                future = asyncio.run_coroutine_threadsafe(
                    self.tts.synthesize(spoken[:1500]), self._loop
                )
                data, _mime = future.result(timeout=60)
                self.tts.play(data)
            except Exception as exc:
                log.info("Sprachausgabe fehlgeschlagen: %s", exc)
            finally:
                if self._speak_queue.empty():
                    self._set_state("bereit" if self.listening else "pausiert")
