"""
Always-on local wake-word detection ("Hey Jarvis") via openWakeWord.

Runs entirely offline against small ONNX models (~4MB total) bundled with
the openwakeword package — no network call, no cloud round-trip, and the
model is loaded once and kept resident for the life of the process (never
reloaded per-detection) per the project's "pre-warm once, never reload per
command" rule.

openWakeWord expects consecutive 80ms frames of 16kHz mono int16 PCM
(1280 samples/frame) fed one at a time; each call to process_frame()
returns the current confidence score for "hey_jarvis" in [0, 1].
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

FRAME_SIZE = 1280        # samples per 80ms frame at 16kHz — openWakeWord's fixed chunk size
SAMPLE_RATE = 16000
WAKE_MODEL_NAME = "hey_jarvis"


@dataclass
class WakeWordConfig:
    threshold: float = 0.5          # score above this on a single frame counts as a trigger
    cooldown_seconds: float = 2.0    # ignore further triggers for this long after one fires


class WakeWordDetector:
    def __init__(self, config: WakeWordConfig | None = None):
        self.config = config or WakeWordConfig()
        self._model = None
        self._last_trigger_time = 0.0

    def load(self) -> None:
        """Loads the ONNX models — the expensive one-time step. Call during
        startup (background thread), not on the first frame of audio."""
        if self._model is not None:
            return
        from openwakeword.model import Model
        self._model = Model(wakeword_models=[WAKE_MODEL_NAME], inference_framework="onnx")
        # Run one dummy frame through so the ONNX runtime session's own
        # first-call warm-up cost (graph optimization, kernel selection)
        # is paid now instead of on the user's first real utterance.
        self._model.predict(np.zeros(FRAME_SIZE, dtype=np.int16))

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def process_frame(self, frame: np.ndarray) -> float:
        """Feed one 80ms int16 PCM frame; returns the current wake-word score."""
        if self._model is None:
            raise RuntimeError("WakeWordDetector.load() must be called before process_frame().")
        if frame.shape[0] != FRAME_SIZE:
            raise ValueError(f"Expected a {FRAME_SIZE}-sample frame, got {frame.shape[0]}.")
        predictions = self._model.predict(frame)
        return float(predictions.get(WAKE_MODEL_NAME, 0.0))

    def check_trigger(self, frame: np.ndarray) -> bool:
        """process_frame() + threshold + cooldown in one call — what the
        pipeline actually wants: "did the wake word just fire, and are we
        past the cooldown from the last time it fired?"."""
        score = self.process_frame(frame)
        now = time.monotonic()
        if score >= self.config.threshold and (now - self._last_trigger_time) >= self.config.cooldown_seconds:
            self._last_trigger_time = now
            return True
        return False

    def reset(self) -> None:
        """Clears openWakeWord's internal rolling audio buffer — call after
        handling a detected wake word so leftover buffered audio doesn't
        cause an immediate re-trigger."""
        if self._model is not None:
            self._model.reset()
