"""Spracherkennung (faster-whisper, lokal), Sprachausgabe (Edge-Stimmen / Windows) und Wake-Word.

Alle schweren Bibliotheken werden erst beim ersten Gebrauch geladen, damit
JARVIS auch ohne Sprachpakete startet.
"""

from __future__ import annotations

import asyncio
import io
import logging
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

#: Abtastrate, die Whisper erwartet.
WHISPER_RATE = 16_000


class SpeechToText:
    def __init__(
        self, model: str, *, device: str = "auto", compute_type: str = "int8", language: str = "de"
    ) -> None:
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self._model: Any = None
        #: Gerät, auf dem das geladene Modell tatsächlich läuft ("cuda"/"cpu").
        self._loaded_device: str | None = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._model is not None:
                return self._model
            from faster_whisper import WhisperModel

            device = self.device
            if device == "auto":
                device = "cuda" if self.compute_type in ("float16", "int8_float16") else "cpu"
            try:
                self._model = WhisperModel(
                    self.model_name, device=device, compute_type=self.compute_type
                )
                self._loaded_device = device
            except Exception as exc:  # CUDA-Bibliotheken fehlen häufig → CPU
                if device == "cpu":
                    raise
                log.warning("Whisper auf GPU nicht möglich (%s) – nutze CPU", exc)
                self._load_cpu()
            return self._model

    def _load_cpu(self) -> None:
        """Lädt das Modell auf der CPU (der Aufrufer hält ``_lock``)."""
        from faster_whisper import WhisperModel

        self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
        self._loaded_device = "cpu"

    def _run(self, model: Any, audio: np.ndarray | str | io.BytesIO) -> str:
        segments, _info = model.transcribe(
            audio,
            language=self.language,
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()

    def transcribe(self, audio: np.ndarray | str | io.BytesIO) -> str:
        model = self._load()
        try:
            return self._run(model, audio)
        except RuntimeError as exc:
            # Fehlen cuBLAS/cuDNN (unter Windows häufig: die CTranslate2-Pakete bringen sie
            # nicht mit), lädt das Modell trotzdem – der Fehler kommt erst beim Erkennen,
            # weil die Segmente erst beim Durchlaufen berechnet werden. → einmalig auf CPU.
            if self._loaded_device == "cpu":
                raise
            log.warning("Whisper auf GPU fehlgeschlagen (%s) – nutze CPU", exc)
            with self._lock:
                if self._loaded_device != "cpu":
                    self._load_cpu()
                model = self._model
            return self._run(model, audio)

    def transcribe_bytes(self, data: bytes, suffix: str = ".webm") -> str:
        # Selbst decodieren statt faster-whispers decode_audio: dessen PyAV-Aufruf passt nicht
        # zu neueren PyAV-Versionen (``metadata_errors`` wurde entfernt).
        samples, _rate = decode_audio(data, rate=WHISPER_RATE)
        if not samples.size:
            return ""
        return self.transcribe(samples)


def decode_audio(data: bytes, rate: int = 24_000) -> tuple[np.ndarray, int]:
    """Decodiert MP3/WAV/WebM/… mit PyAV zu Mono-Float32 mit der gewünschten Abtastrate."""
    import av

    with av.open(io.BytesIO(data), mode="r") as container:
        if not container.streams.audio:
            return np.zeros(0, dtype=np.float32), rate
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="flt", layout="mono", rate=rate)
        chunks = []
        for frame in container.decode(stream):
            for converted in resampler.resample(frame):
                chunks.append(converted.to_ndarray().reshape(-1))
        for converted in resampler.resample(None):
            chunks.append(converted.to_ndarray().reshape(-1))
    if not chunks:
        return np.zeros(0, dtype=np.float32), rate
    return np.concatenate(chunks).astype(np.float32), rate


class TextToSpeech:
    def __init__(self, engine: str, voice: str) -> None:
        self.engine = engine
        self.voice = voice
        self._playing = threading.Event()

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        """Erzeugt Audio. Liefert ``(daten, mime-typ)``."""
        if self.engine == "edge":
            try:
                import edge_tts

                communicate = edge_tts.Communicate(text, self.voice)
                audio = bytearray()
                async for chunk in communicate.stream():
                    if chunk.get("type") == "audio" and chunk.get("data"):
                        audio.extend(chunk["data"])
                if audio:
                    return bytes(audio), "audio/mpeg"
            except Exception as exc:
                log.info("Edge-TTS nicht verfügbar (%s) – nutze Windows-Stimme", exc)
        return await asyncio.to_thread(self._sapi, text), "audio/wav"

    @staticmethod
    def _sapi(text: str) -> bytes:
        if sys.platform == "win32":
            # SAPI läuft über COM, und COM muss in jedem Thread initialisiert sein – sonst
            # "CoInitialize has not been called". ``asyncio.to_thread`` nutzt wechselnde
            # Pool-Threads; ein erneuter Aufruf im selben Thread ist unschädlich.
            import ctypes

            ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
        import pyttsx3

        engine = pyttsx3.init()
        for voice in engine.getProperty("voices"):
            if (
                "de" in str(getattr(voice, "languages", "")).lower()
                or "german" in voice.name.lower()
                or "deutsch" in voice.name.lower()
            ):
                engine.setProperty("voice", voice.id)
                break
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            path = handle.name
        try:
            engine.save_to_file(text, path)
            engine.runAndWait()
            return Path(path).read_bytes()
        finally:
            Path(path).unlink(missing_ok=True)

    def play(self, data: bytes) -> None:
        """Spielt Audio auf den PC-Lautsprechern ab (blockiert bis zum Ende)."""
        import sounddevice as sd

        samples, rate = decode_audio(data)
        if samples.size == 0:
            return
        self._playing.set()
        try:
            sd.play(samples, rate)
            sd.wait()
        finally:
            self._playing.clear()

    @property
    def playing(self) -> bool:
        return self._playing.is_set()

    def stop(self) -> None:
        try:
            import sounddevice as sd

            sd.stop()
        except Exception:
            pass


class WakeWord:
    def __init__(self, name: str, threshold: float) -> None:
        self.name = name
        self.threshold = threshold
        self._model: Any = None

    def load(self) -> None:
        import openwakeword
        from openwakeword.model import Model

        openwakeword.utils.download_models([self.name])
        self._model = Model(wakeword_models=[self.name], inference_framework="onnx")

    def detect(self, frame: np.ndarray) -> bool:
        if self._model is None:
            self.load()
        scores = self._model.predict(frame)
        if max(scores.values(), default=0.0) >= self.threshold:
            self._model.reset()
            return True
        return False
