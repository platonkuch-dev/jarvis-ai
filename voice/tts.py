"""
Local, GPU-accelerated text-to-speech for the fast path.

Backend: facebook/mms-tts-rus (Meta's Massively Multilingual Speech project),
via HuggingFace transformers. Chosen after two other candidates failed for
concrete, verified reasons on this machine, not preference:
  - piper-tts: reproducible upstream bug in the Windows wheel (the compiled
    espeakbridge extension ignores the espeak_data_dir argument and looks
    for phoneme data at a hardcoded CI build path that doesn't exist here).
  - Silero TTS (torch.hub snakers4/silero-models): model weights are hosted
    at models.silero.ai, which is network-unreachable from this machine
    (connection timeout, confirmed via a direct curl, not a code issue).
mms-tts-rus is hosted on huggingface.co (confirmed reachable — faster-whisper
already pulls from there), loads once in ~7s, and synthesizes at roughly 10x
real-time on an RTX 5070.

Defines a small TTSProvider protocol so the engine can be swapped later
(e.g. if a lower-latency or higher-quality model becomes the better choice)
without touching the fast-path pipeline that calls it.
"""
from __future__ import annotations

import threading
from typing import Protocol

import numpy as np


class TTSProvider(Protocol):
    def load(self) -> None:
        """Pay the one-time model-load cost — call during startup, not on
        the first spoken response."""
        ...

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        """Returns (int16 PCM mono samples, sample_rate). No playback."""
        ...

    def speak(self, text: str) -> None:
        """Synthesize and play through the default output device, blocking
        until playback finishes (or stop() is called)."""
        ...

    def stop(self) -> None:
        """Cut off in-progress playback immediately — barge-in support."""
        ...


class MmsTtsProvider:
    MODEL_ID = "facebook/mms-tts-rus"

    def __init__(self, device: str = "cuda"):
        self.device = device
        self._model = None
        self._tokenizer = None
        self._sample_rate: int | None = None
        self._stream = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    def load(self) -> None:
        if self._model is not None:
            return
        import torch
        from transformers import VitsModel, AutoTokenizer

        device = self.device if torch.cuda.is_available() else "cpu"
        self.device = device
        self._model = VitsModel.from_pretrained(self.MODEL_ID).to(device)
        self._tokenizer = AutoTokenizer.from_pretrained(self.MODEL_ID)
        self._sample_rate = self._model.config.sampling_rate
        # Warm-up pass: first GPU inference pays kernel-selection/cuDNN
        # autotune cost — do that now, not on the first real response.
        self.synthesize("Готов.")

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        if self._model is None or self._tokenizer is None:
            raise RuntimeError("MmsTtsProvider.load() must be called first.")
        import torch

        text = text.strip()
        if not text:
            return np.zeros(0, dtype=np.int16), self._sample_rate or 16000

        inputs = self._tokenizer(text, return_tensors="pt").to(self.device)
        with torch.no_grad():
            waveform = self._model(**inputs).waveform
        audio = waveform.squeeze().detach().cpu().numpy()
        pcm16 = (audio * 32767.0).clip(-32768, 32767).astype(np.int16)
        return pcm16, self._sample_rate

    def speak(self, text: str) -> None:
        import sounddevice as sd

        pcm16, sample_rate = self.synthesize(text)
        if pcm16.size == 0:
            return

        self._stop_event.clear()
        with self._lock:
            self._stream = sd.OutputStream(samplerate=sample_rate, channels=1, dtype="int16")
            self._stream.start()

        chunk_size = sample_rate // 10  # ~100ms chunks so stop() can cut in quickly
        try:
            for start in range(0, len(pcm16), chunk_size):
                if self._stop_event.is_set():
                    break
                chunk = pcm16[start:start + chunk_size]
                self._stream.write(chunk)
        finally:
            with self._lock:
                if self._stream is not None:
                    self._stream.stop()
                    self._stream.close()
                    self._stream = None

    def stop(self) -> None:
        self._stop_event.set()
