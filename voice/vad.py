"""
Voice Activity Detection — used AFTER the wake word fires, to know when the
user has finished speaking their command so recording can stop and hand off
to STT immediately, instead of a fixed "record for N seconds" guess.

Wraps the same Silero VAD ONNX model openwakeword already bundles (so no
extra download), via openwakeword.VAD's thin onnxruntime wrapper.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

VAD_FRAME_SIZE = 480  # 30ms at 16kHz — Silero VAD's own recommended chunk size


@dataclass
class SilenceConfig:
    speech_threshold: float = 0.5       # VAD score above this counts as "speech present"
    # Real latency measurement found this trailing-silence wait was, by
    # itself, LARGER than the entire STT+router+action chain that follows it
    # (62-375ms measured end to end vs. 700ms of dead air here) — the single
    # biggest lever in the whole Fast Path. 450ms is still comfortably above
    # a natural word-to-word pause but well below the ~600-800ms a person
    # tends to leave before starting a genuinely NEW sentence, so short
    # Fast Path commands ("громче", "сделай скриншот") aren't cut mid-word.
    silence_ms_to_end: float = 450.0
    max_utterance_ms: float = 12000.0   # hard safety cap so a stuck-open mic can't record forever


class EndOfSpeechDetector:
    def __init__(self, config: SilenceConfig | None = None):
        self.config = config or SilenceConfig()
        self._vad = None
        self._heard_speech = False
        self._silence_ms = 0.0
        self._elapsed_ms = 0.0

    def load(self) -> None:
        if self._vad is not None:
            return
        from openwakeword import VAD
        self._vad = VAD()

    def start_utterance(self) -> None:
        """Call once when recording starts (right after the wake word fires)."""
        if self._vad is not None:
            self._vad.reset_states()
        self._heard_speech = False
        self._silence_ms = 0.0
        self._elapsed_ms = 0.0

    def feed(self, frame: np.ndarray, frame_ms: float) -> bool:
        """
        Feed one chunk of int16 PCM audio (any length — internally re-chunked
        to VAD_FRAME_SIZE). Returns True once the utterance should be
        considered finished (real trailing silence, or the safety cap hit).
        """
        if self._vad is None:
            raise RuntimeError("EndOfSpeechDetector.load() must be called first.")

        score = self._vad.predict(frame, frame_size=VAD_FRAME_SIZE)
        self._elapsed_ms += frame_ms

        if score >= self.config.speech_threshold:
            self._heard_speech = True
            self._silence_ms = 0.0
        else:
            self._silence_ms += frame_ms

        if self._elapsed_ms >= self.config.max_utterance_ms:
            return True
        if self._heard_speech and self._silence_ms >= self.config.silence_ms_to_end:
            return True
        return False
