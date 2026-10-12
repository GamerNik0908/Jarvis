"""Audio-Hilfen: Sprachende erkennen (einfache Energie-Erkennung) und Töne erzeugen."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable

import numpy as np

SAMPLE_RATE = 16_000
FRAME = 1280  # 80 ms – Blockgröße von openWakeWord


def rms(frame: np.ndarray) -> float:
    if frame.size == 0:
        return 0.0
    data = frame.astype(np.float32)
    return float(np.sqrt(np.mean(data * data)))


class EndOfSpeech:
    """Erkennt Anfang und Ende einer Äußerung anhand der Lautstärke.

    Die Schwelle passt sich dem Raumgeräusch der ersten Blöcke an.
    """

    def __init__(
        self,
        *,
        silence_s: float = 0.9,
        max_s: float = 20.0,
        start_timeout_s: float = 6.0,
        min_threshold: float = 350.0,
    ) -> None:
        self.frames_per_s = SAMPLE_RATE / FRAME
        self.silence_frames = math.ceil(silence_s * self.frames_per_s)
        self.max_frames = math.ceil(max_s * self.frames_per_s)
        self.start_timeout_frames = math.ceil(start_timeout_s * self.frames_per_s)
        self.min_threshold = min_threshold
        self.reset()

    def reset(self) -> None:
        self.frames: list[np.ndarray] = []
        self.noise: list[float] = []
        self.started = False
        self.quiet = 0
        self.count = 0

    @property
    def threshold(self) -> float:
        baseline = float(np.median(self.noise)) if self.noise else 0.0
        return max(self.min_threshold, baseline * 3.0)

    def feed(self, frame: np.ndarray) -> str:
        """``continue`` | ``done`` (Sprache beendet) | ``timeout`` (nie gesprochen)."""
        self.count += 1
        level = rms(frame)
        self.frames.append(frame)
        if not self.started:
            if self.count <= 4:
                self.noise.append(level)
            if level > self.threshold and self.count > 2:
                self.started = True
                self.quiet = 0
            elif self.count >= self.start_timeout_frames:
                return "timeout"
            return "continue"
        self.quiet = self.quiet + 1 if level < self.threshold else 0
        if self.quiet >= self.silence_frames or self.count >= self.max_frames:
            return "done"
        return "continue"

    def audio(self) -> np.ndarray:
        if not self.frames:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(self.frames).astype(np.float32) / 32768.0


def capture(
    frames: Iterable[np.ndarray], detector: EndOfSpeech, stop: Callable[[], bool]
) -> np.ndarray | None:
    """Liest Blöcke, bis die Äußerung endet. ``None`` = niemand hat gesprochen."""
    detector.reset()
    for frame in frames:
        if stop():
            return None
        state = detector.feed(frame)
        if state == "timeout":
            return None
        if state == "done":
            return detector.audio()
    return detector.audio() if detector.started else None


def beep(
    frequency: float = 880.0, duration_s: float = 0.12, volume: float = 0.25, rate: int = 24_000
) -> np.ndarray:
    t = np.linspace(0, duration_s, int(rate * duration_s), endpoint=False)
    envelope = np.minimum(1.0, np.minimum(t, duration_s - t) * 40)
    tone: np.ndarray = (np.sin(2 * np.pi * frequency * t) * envelope * volume).astype(np.float32)
    return tone
