"""Spoken wake word ("Hey Jarvis") while the agent sleeps.

openWakeWord's pre-trained "hey_jarvis" model, run directly on onnxruntime
(already a dependency) instead of through the openwakeword package, which
would drag scipy + scikit-learn (~160 MB) into the installer. The three
.onnx files live in wake_models/ -- no download, no network, no LLM, a few %
of one CPU core. The pipeline mirrors openwakeword.utils.AudioFeatures:

  16 kHz int16, 80 ms chunks -> melspectrogram.onnx (x/10 + 2)
  -> last 76 mel frames -> embedding_model.onnx (96-d, one per chunk)
  -> last 16 embeddings -> hey_jarvis.onnx -> score 0..1

The microphone stream is opened only while asleep and closed on wake, so it
never competes with the conversation's audio. Anything missing (models,
microphone) just logs once and leaves F10 as the only way to wake.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

import config

logger = logging.getLogger("jarvis-voice-agent.wake_word")

MODELS_DIR = Path(__file__).resolve().parent / "wake_models"
SAMPLE_RATE = 16000
CHUNK = 1280  # 80 ms -- the frame size the models are built around
_MEL_CONTEXT = 160 * 3  # extra samples so each chunk's mel frames line up
_MEL_WINDOW = 76
_N_EMBEDDINGS = 16
_COOLDOWN_S = 2.0


def _model_path(name: str) -> Path:
    matches = sorted(MODELS_DIR.glob(f"{name}*.onnx"))
    if not matches:
        raise FileNotFoundError(f"{MODELS_DIR / name}*.onnx")
    return matches[-1]


class WakeWordDetector:
    """Streaming scorer: feed 1280-sample int16 chunks, get a 0..1 score each."""

    def __init__(self, model: str = "hey_jarvis") -> None:
        import numpy as np
        import onnxruntime as ort

        self._np = np
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1

        def session(path: Path):
            return ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])

        self._mel = session(_model_path("melspectrogram"))
        self._emb = session(_model_path("embedding_model"))
        self._ww = session(_model_path(model))
        self._mel_in = self._mel.get_inputs()[0].name
        self._emb_in = self._emb.get_inputs()[0].name
        self._ww_in = self._ww.get_inputs()[0].name
        self.reset()

    def reset(self) -> None:
        np = self._np
        self._raw: deque[int] = deque(maxlen=SAMPLE_RATE * 2)
        self._mels = np.ones((_MEL_WINDOW, 32), dtype=np.float32)
        self._embeddings: deque = deque(maxlen=_N_EMBEDDINGS)

    def score(self, chunk) -> float:
        np = self._np
        chunk = np.asarray(chunk, dtype=np.int16).reshape(-1)
        self._raw.extend(chunk.tolist())
        audio = np.array(list(self._raw)[-(len(chunk) + _MEL_CONTEXT):], dtype=np.float32)[None, :]
        mel = np.squeeze(self._mel.run(None, {self._mel_in: audio})[0]) / 10 + 2
        self._mels = np.vstack((self._mels, mel))[-_MEL_WINDOW * 2:]
        window = self._mels[-_MEL_WINDOW:].astype(np.float32)[None, :, :, None]
        self._embeddings.append(np.squeeze(self._emb.run(None, {self._emb_in: window})[0]))
        if len(self._embeddings) < _N_EMBEDDINGS:
            return 0.0  # ~1.3 s of audio before the first real score
        features = np.stack(self._embeddings)[None, :, :].astype(np.float32)
        return float(np.squeeze(self._ww.run(None, {self._ww_in: features})[0]))


def _input_device() -> int | str | None:
    raw = (config.AUDIO_INPUT_DEVICE or "").strip()
    if not raw:
        return None
    return int(raw) if raw.isdigit() else raw


class WakeWordListener:
    def __init__(self, on_detect: Callable[[], None]) -> None:
        self._on_detect = on_detect
        self._detector: WakeWordDetector | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._unavailable = False

    @property
    def available(self) -> bool:
        return config.WAKE_WORD_ENABLED and not self._unavailable

    def start(self) -> None:
        if not self.available or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wake-word", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            import sounddevice as sd

            if self._detector is None:
                self._detector = WakeWordDetector(config.WAKE_WORD_MODEL)
        except Exception as exc:
            self._unavailable = True
            logger.warning("wake word disabled (%s); F10 still wakes Jarvis", exc)
            return

        detector = self._detector
        detector.reset()
        last_hit = 0.0
        try:
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                blocksize=CHUNK, device=_input_device()) as stream:
                logger.info("listening for wake word %r", config.WAKE_WORD_MODEL)
                while not self._stop.is_set():
                    frames, _overflow = stream.read(CHUNK)
                    score = detector.score(frames)
                    if score >= config.WAKE_WORD_THRESHOLD and time.time() - last_hit > _COOLDOWN_S:
                        last_hit = time.time()
                        logger.info("wake word detected (score %.2f)", score)
                        self._stop.set()
                        self._on_detect()
        except Exception as exc:
            logger.warning("wake word microphone stream failed: %s", exc)
