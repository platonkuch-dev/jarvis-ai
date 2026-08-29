"""
Standalone, runnable proof of the Fast Path pipeline against a REAL
microphone: Wake Word -> VAD-gated recording -> STT -> Fast Router -> local
TTS. Claude/Gemini is never involved for anything that matches a fast rule.

This is deliberately NOT wired into main.py's Gemini Live loop yet — see
voice/README.md for why and what the next integration step looks like.
Run this directly to try the fast path with your own voice:

    python -m voice.fast_path_demo

Say "Hey Jarvis" (the wake word is English — openWakeWord's pretrained
"hey_jarvis" model, no custom training done), wait for the beep-equivalent
(a log line), then say a command in Russian, e.g. "громкость 30",
"сделай скриншот", "открой блокнот", "сверни окно".

Ctrl+C to stop.
"""
from __future__ import annotations

import sys
import time

import numpy as np
import sounddevice as sd

from core import latency
from voice.pipeline import VoicePipeline
from voice.wake_word import FRAME_SIZE, SAMPLE_RATE


def main() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                # line_buffering=True matters here specifically: stdout is
                # fully-buffered (not line-buffered) whenever it's not a
                # real TTY — which is exactly the case when this script is
                # launched with output redirected to a log file. Without
                # this, every print() below sits in an internal buffer and
                # never reaches the file, making a perfectly healthy
                # "listening for the wake word" process look hung.
                stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            except Exception:
                pass

    print("[FastPathDemo] Loading voice models (wake word, VAD, STT, TTS)...")
    pipeline = VoicePipeline()
    t0 = time.monotonic()
    pipeline.prewarm()
    print(f"[FastPathDemo] Ready in {time.monotonic() - t0:.1f}s. Say 'Hey Jarvis' then a command.")
    print("[FastPathDemo] Ctrl+C to stop.\n")

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME_SIZE) as stream:
        try:
            while True:
                frame, _overflow = stream.read(FRAME_SIZE)
                frame = frame.reshape(-1).astype(np.int16)

                if pipeline.wake_word.check_trigger(frame):
                    pipeline.wake_word.reset()
                    print("[FastPathDemo] Wake word detected — listening for a command...")
                    audio = pipeline.record_utterance(stream)
                    duration_s = len(audio) / SAMPLE_RATE
                    print(f"[FastPathDemo] Recorded {duration_s:.1f}s, transcribing...")

                    result = pipeline.process_utterance(audio)
                    if not result.transcript:
                        print("[FastPathDemo] Heard nothing usable.\n")
                        continue

                    print(f"[FastPathDemo] Heard: {result.transcript!r}")
                    if result.fast_path:
                        rr = result.route_result
                        print(f"[FastPathDemo] FAST PATH matched '{rr.rule_name}' -> {rr.message}\n")
                    else:
                        print("[FastPathDemo] No fast-path match — this would go to the Smart Path (Claude) in the full pipeline.\n")
        except KeyboardInterrupt:
            print("\n[FastPathDemo] Stopped.")
            latency.print_summary()


if __name__ == "__main__":
    main()
