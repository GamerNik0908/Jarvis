"""Spracherkennung (faster-whisper, lokal), Sprachausgabe (Edge-Stimmen / Windows) und Wake-Word.

Alle schweren Bibliotheken werden erst beim ersten Gebrauch geladen, damit
JARVIS auch ohne Sprachpakete startet.
"""

from __future__ import annotations

import asyncio
import io
import logging
import tempfile
import threading
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)


class SpeechToText:
    def __init__(
        self, model: str, *, device: str = "auto", compute_type: str = "int8", language: str = "de"
    ) -> None:
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self._model: Any = None
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
            except Exception as exc:  # CUDA-Bibliotheken fehlen häufig → CPU
                if device == "cpu":
                    raise
                log.warning("Whisper auf GPU nicht möglich (%s) – nutze CPU", exc)
                self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
            return self._model

    def transcribe(self, audio: np.ndarray | str | io.BytesIO) -> str:
        model = self._load()
        segments, _info = model.transcribe(
            audio,
            language=self.language,
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()

    def transcribe_bytes(self, data: bytes, suffix: str = ".webm") -> str:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(data)
            path = handle.name
        try:
            return self.transcribe(path)
        finally:
            Path(path).unlink(missing_ok=True)


def decode_audio(data: bytes) -> tuple[np.ndarray, int]:
    """Decodiert MP3/WAV/… mit PyAV zu Mono-Float32."""
    import av

    with av.open(io.BytesIO(data)) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="flt", layout="mono", rate=24_000)
        chunks = []
        for frame in container.decode(stream):
            for converted in resampler.resample(frame):
                chunks.append(converted.to_ndarray().reshape(-1))
        for converted in resampler.resample(None):
            chunks.append(converted.to_ndarray().reshape(-1))
    if not chunks:
        return np.zeros(0, dtype=np.float32), 24_000
    return np.concatenate(chunks).astype(np.float32), 24_000


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
