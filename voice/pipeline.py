"""
The Fast Path pipeline, end to end:

    Microphone -> Wake Word -> VAD-gated recording -> STT -> Fast Router
        -> [matched] Rust/Python tool, spoken confirmation via local TTS
        -> [no match] handed back to the caller (main.py) as plain text,
           to go through the existing Gemini Live Smart Path unchanged

This module owns exactly one thing: turning microphone audio into either
(a) a directly-executed action + spoken response, with Claude/Gemini never
entering the picture, or (b) a text string ready to hand to the existing
smart path. It does NOT own the smart path itself — main.py's JarvisLive
still does that, exactly as before.

All four heavy components (wake word, VAD, STT, TTS) are loaded once via
prewarm() and kept resident — see core/latency.py's "never reload a model
per command" rule applied to voice this time instead of just tools.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass

import numpy as np

from voice.wake_word import WakeWordDetector, WakeWordConfig, FRAME_SIZE, SAMPLE_RATE
from voice.vad import EndOfSpeechDetector, SilenceConfig
from voice.stt import SpeechToText, pcm16_to_float32
from voice.tts import MmsTtsProvider
from voice.fast_router import route, RouteResult
from core import latency


@dataclass
class PipelineResult:
    transcript: str
    fast_path: bool
    route_result: RouteResult | None = None


class VoicePipeline:
    """
    Owns the four voice models. Two ways to use it:

      1. Call prewarm() once at startup (background thread) so all four
         models are loaded and warmed before anything needs them.
      2. Call process_utterance(pcm16_audio) with a complete recorded
         utterance (wake-word-to-silence) to get transcript + fast-path
         result — used by both the live microphone loop (pipeline_loop.py,
         not included in this pass) and the offline self-test.

    Deliberately does NOT own the microphone stream itself — capturing
    audio is main.py's job today (sounddevice.InputStream already runs
    there for the existing Gemini Live path); this class is the part that
    turns captured frames into an action, independent of where the frames
    came from.
    """

    def __init__(self):
        self.wake_word = WakeWordDetector(WakeWordConfig())
        self.vad = EndOfSpeechDetector(SilenceConfig())
        self.stt = SpeechToText()
        self.tts = MmsTtsProvider()
        self._prewarmed = False

    def prewarm(self) -> None:
        if self._prewarmed:
            return
        t0 = time.monotonic()
        self.wake_word.load()
        self.vad.load()
        self.stt.load()
        self.tts.load()
        self._prewarmed = True
        latency.record("voice_pipeline_prewarm", (time.monotonic() - t0) * 1000)

    @property
    def is_ready(self) -> bool:
        return self._prewarmed

    def transcribe(self, pcm16_audio: np.ndarray) -> str:
        t0 = time.monotonic()
        text = self.stt.transcribe(pcm16_to_float32(pcm16_audio))
        latency.record("stt", (time.monotonic() - t0) * 1000)
        return text

    def process_utterance(self, pcm16_audio: np.ndarray) -> PipelineResult:
        """Full fast-path attempt for one already-recorded utterance."""
        text = self.transcribe(pcm16_audio)
        if not text:
            return PipelineResult(transcript="", fast_path=False)

        t0 = time.monotonic()
        result = route(text)
        latency.record("fast_router", (time.monotonic() - t0) * 1000)

        if result.matched:
            t0 = time.monotonic()
            self.tts.speak(result.message)
            latency.record("tts", (time.monotonic() - t0) * 1000)
            return PipelineResult(transcript=text, fast_path=True, route_result=result)

        return PipelineResult(transcript=text, fast_path=False, route_result=result)

    def record_utterance(self, stream, max_seconds: float = 12.0) -> np.ndarray:
        """
        Reads frames from an already-open sounddevice InputStream until
        EndOfSpeechDetector says the user has stopped talking (real
        trailing-silence detection, not a fixed sleep). Returns the
        recorded int16 PCM samples.
        """
        self.vad.start_utterance()
        chunks: list[np.ndarray] = []
        frame_ms = FRAME_SIZE / SAMPLE_RATE * 1000

        deadline = time.monotonic() + max_seconds
        while time.monotonic() < deadline:
            frame, _overflow = stream.read(FRAME_SIZE)
            frame = frame.reshape(-1).astype(np.int16)
            chunks.append(frame)
            if self.vad.feed(frame, frame_ms):
                break

        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)
