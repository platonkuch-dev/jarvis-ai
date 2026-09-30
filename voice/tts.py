"""
Local text-to-speech for the fast path.

Default backend: PiperTtsProvider, a standalone Piper binary (voice/piper_bin/)
running the ru_RU-ruslan-medium voice — CPU-only ONNX inference, no GPU
needed, measured real-time factor ~0.04 (25x faster than real-time) on this
machine. NOT the piper-tts PYTHON package: that package's compiled
espeakbridge extension has a genuine upstream bug on Windows (both 1.6.1 and
1.7.0) — it ignores the espeak_data_dir argument and hardcodes a CI build
path that doesn't exist on any real machine, confirmed via direct testing.
The standalone binary release (github.com/rhasspy/piper, tag 2023.11.14-2)
bundles its own espeak-ng-data directory alongside the executable and
sidesteps that broken bridge entirely.

Kept as a second option: MmsTtsProvider (facebook/mms-tts-rus via HuggingFace
transformers, GPU-accelerated) — the previous default, before Piper's
Windows-binary route was found. Silero TTS was also tried and abandoned:
its canonical host, models.silero.ai, is network-unreachable from this
machine (connection timeout, confirmed via direct curl, re-confirmed months
later — not a fluke).

Defines a small TTSProvider protocol so the engine can be swapped later
without touching the fast-path pipeline that calls it.
"""
from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
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

        # Cleared BEFORE synthesize(), not after: a stop() call arriving
        # while synthesis is still running (a real possibility — Piper's
        # subprocess-per-call synthesis is not instant) must still be
        # honored once playback would otherwise start. Clearing after
        # synthesize() silently discarded exactly that stop() call, found
        # via a real barge-in timing test that behaved unexpectedly.
        self._stop_event.clear()
        pcm16, sample_rate = self.synthesize(text)
        if pcm16.size == 0 or self._stop_event.is_set():
            return

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


class PiperTtsProvider:
    """Local TTS via the standalone Piper binary in voice/piper_bin/ — see
    module docstring for why this is a subprocess call to a bundled .exe
    rather than the (broken, on Windows) piper-tts pip package.

    No model object to keep resident: each synthesize() call is a fresh,
    short-lived `piper.exe` process. This is deliberate, not a missed
    optimization — process startup + model load together measured ~200ms,
    and the whole call (startup + inference) for a multi-second utterance
    still lands under 300ms, so a persistent process would save very little
    while adding real complexity (stdin/stdout framing, restart-on-crash).
    """

    BIN_DIR = Path(__file__).parent / "piper_bin"

    def __init__(self, voice: str = "ru_RU-ruslan-medium"):
        self.voice = voice
        self._exe = self.BIN_DIR / "piper.exe"
        self._model = self.BIN_DIR / "voices" / f"{voice}.onnx"
        self._config = self.BIN_DIR / "voices" / f"{voice}.onnx.json"
        self._espeak_data = self.BIN_DIR / "espeak-ng-data"
        self._sample_rate: int | None = None
        self._stream = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    def load(self) -> None:
        if self._sample_rate is not None:
            return
        if not self._exe.exists():
            raise RuntimeError(f"piper.exe not found at {self._exe}")
        if not self._model.exists():
            raise RuntimeError(f"Piper voice model not found at {self._model}")
        cfg = json.loads(self._config.read_text(encoding="utf-8"))
        self._sample_rate = cfg["audio"]["sample_rate"]
        # Warm-up pass — pays first-process-spawn cost now, not on the
        # user's first real response.
        self.synthesize("Готов.")

    @property
    def is_loaded(self) -> bool:
        return self._sample_rate is not None

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        if self._sample_rate is None:
            raise RuntimeError("PiperTtsProvider.load() must be called first.")
        text = text.strip()
        if not text:
            return np.zeros(0, dtype=np.int16), self._sample_rate

        # Model/espeak-data paths MUST be relative to cwd (BIN_DIR), not
        # absolute — this project's absolute path contains Cyrillic
        # characters and parentheses ("...Mark-XLVIII-main (2)..."), and
        # piper's bundled onnxruntime/espeak-ng DLLs crash instantly
        # (STATUS_STACK_BUFFER_OVERRUN, no stderr output at all) when that
        # absolute path is passed as a command-line argument — reproducibly,
        # confirmed by isolating every other variable (stdin/stdout/stderr
        # as pipes vs real files, shell=True vs False, quiet vs verbose).
        # The exact same path works fine as `cwd` or as a relative argument
        # once cwd is already there. Only the executable itself needs an
        # absolute path, since Windows won't resolve a bare relative exe
        # name against cwd the way a shell does.
        result = subprocess.run(
            [
                str(self._exe), "-m", "voices/" + self._model.name,
                "--espeak_data", "espeak-ng-data",
                "--output_raw", "-q",
            ],
            input=text.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=str(self.BIN_DIR),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        pcm16 = np.frombuffer(result.stdout, dtype=np.int16)
        return pcm16, self._sample_rate

    def speak(self, text: str) -> None:
        import sounddevice as sd

        # Cleared BEFORE synthesize(), not after: a stop() call arriving
        # while synthesis is still running (a real possibility — Piper's
        # subprocess-per-call synthesis is not instant) must still be
        # honored once playback would otherwise start. Clearing after
        # synthesize() silently discarded exactly that stop() call, found
        # via a real barge-in timing test that behaved unexpectedly.
        self._stop_event.clear()
        pcm16, sample_rate = self.synthesize(text)
        if pcm16.size == 0 or self._stop_event.is_set():
            return

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
