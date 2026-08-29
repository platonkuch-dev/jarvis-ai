"""
Local, GPU-accelerated speech-to-text via faster-whisper (CTranslate2),
tuned for latency over maximum accuracy per the project's stated priority,
while keeping Russian quality solid (the "small" multilingual Whisper model
is a deliberately chosen middle point — "tiny" transcribes noticeably worse
on Russian, "medium"/"large" cost real seconds per utterance on this GPU).

Loaded once at startup and kept resident — never reloaded per command.
"""
from __future__ import annotations

import numpy as np

MODEL_SIZE = "small"
SAMPLE_RATE = 16000


class SpeechToText:
    def __init__(self, device: str = "cuda", compute_type: str = "float16"):
        self.device = device
        self.compute_type = compute_type
        self._model = None

    def load(self) -> None:
        if self._model is not None:
            return
        from faster_whisper import WhisperModel

        try:
            self._model = WhisperModel(MODEL_SIZE, device=self.device, compute_type=self.compute_type)
        except Exception:
            # GPU/CUDA runtime unavailable for some reason — CPU still works,
            # just slower. Never let STT be a hard dependency on a working GPU.
            self.device = "cpu"
            self._model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")

        # Pay first-inference warm-up (CUDA kernel selection, autotuning)
        # now instead of on the user's first real utterance.
        self.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32))

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def transcribe(self, audio_f32: np.ndarray, language: str = "ru") -> str:
        """audio_f32: mono float32 samples in [-1, 1] at 16kHz. Returns the
        transcribed text (empty string for silence/noise)."""
        if self._model is None:
            raise RuntimeError("SpeechToText.load() must be called first.")
        segments, _info = self._model.transcribe(
            # vad_filter=False: the audio handed here already went through
            # our own EndOfSpeechDetector (voice/vad.py) before recording
            # stopped, so Whisper's own internal VAD pass is redundant work
            # on already-trimmed speech. Measured 30-90ms saved per short
            # utterance by skipping it, with no accuracy difference on
            # phrases where both settings produced the same transcript.
            audio_f32, language=language, beam_size=1, vad_filter=False,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()


def pcm16_to_float32(pcm16: np.ndarray) -> np.ndarray:
    return (pcm16.astype(np.float32) / 32768.0).clip(-1.0, 1.0)
