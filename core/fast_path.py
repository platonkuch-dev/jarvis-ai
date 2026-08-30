"""
Fast Path: local wake word / STT / router / TTS, bypassing Gemini Live
entirely for a small set of zero-latency commands.

Split out of main.py (Stage 2 module split, see REWORK_PLAN.md) with no
behavior changes -- these were previously JarvisLive methods in main.py.
Each function takes the JarvisLive instance as `self`, exactly as when it
was a bound method; JarvisLive keeps thin wrapper methods of the same name
(minus the module qualifier) so every call site elsewhere is unchanged.
"""
from __future__ import annotations

import asyncio
import time
import traceback

import numpy as np

from core import latency
from core.audio_pipeline import SEND_SAMPLE_RATE
from voice.wake_word import FRAME_SIZE as FAST_PATH_FRAME_SIZE
from voice.fast_router import route as fast_route, RouteResult, INTENT_DISPATCH as FAST_PATH_INTENT_DISPATCH


async def run_fast_path(self) -> None:
    """
    Consumes the raw audio the mic callback fans out, independent of
    whatever Gemini is doing. Re-slices arbitrary-sized input blocks
    into the fixed 1280-sample (80ms) frames openWakeWord/Silero VAD
    require, runs wake-word detection on every frame while idle, and
    once triggered, records until real trailing silence (not a fixed
    sleep) before handing the utterance off for transcription+routing.
    """
    self._fast_path_queue = asyncio.Queue()
    frame_buffer = np.zeros(0, dtype=np.int16)
    recording_frames: list[np.ndarray] = []
    frame_ms = FAST_PATH_FRAME_SIZE / SEND_SAMPLE_RATE * 1000

    while True:
        block = await self._fast_path_queue.get()
        samples = block.reshape(-1).astype(np.int16)
        frame_buffer = np.concatenate([frame_buffer, samples])

        while len(frame_buffer) >= FAST_PATH_FRAME_SIZE:
            frame, frame_buffer = frame_buffer[:FAST_PATH_FRAME_SIZE], frame_buffer[FAST_PATH_FRAME_SIZE:]

            with self._fast_path_lock:
                state = self._fast_path_state

            if state == "idle":
                if not self._voice_pipeline.is_ready:
                    continue  # still prewarming — don't attempt detection on a half-loaded model
                try:
                    triggered = await asyncio.to_thread(self._voice_pipeline.wake_word.check_trigger, frame)
                except Exception as e:
                    print(f"[FastPath] Wake word error: {e}")
                    continue
                if triggered:
                    # Barge-in for local TTS: the wake-word check keeps
                    # running even while a Fast Path response is still
                    # playing (this branch only requires state=="idle",
                    # which _handle_fast_path_utterance() already
                    # restores before its TTS call starts — see below).
                    # A fresh "Hey Jarvis" mid-sentence should cut the
                    # old response off immediately rather than let it
                    # keep talking over the new command. stop() is a
                    # cheap no-op if nothing is currently playing.
                    self._voice_pipeline.tts.stop()
                    self._voice_pipeline.wake_word.reset()
                    self._voice_pipeline.vad.start_utterance()
                    recording_frames = []
                    with self._fast_path_lock:
                        self._fast_path_state = "recording"
                    self.ui.write_log("SYS: Fast Path — wake word detected, listening...")
                    latency.record("fast_path_wake_word", 0.0)

            elif state == "recording":
                recording_frames.append(frame)
                try:
                    done = await asyncio.to_thread(self._voice_pipeline.vad.feed, frame, frame_ms)
                except Exception as e:
                    print(f"[FastPath] VAD error: {e}")
                    done = True
                if done:
                    with self._fast_path_lock:
                        self._fast_path_state = "idle"
                    audio = np.concatenate(recording_frames) if recording_frames else np.zeros(0, dtype=np.int16)
                    recording_frames = []
                    asyncio.create_task(self._handle_fast_path_utterance(audio))

async def handle_fast_path_utterance(self, audio: np.ndarray) -> None:
    # This method only ever runs as a bare asyncio.create_task(...) (see
    # the wake-word branch above) — nothing awaits it or reads its
    # result. Before this try/except, an exception from transcribe()
    # or fast_route() (e.g. a corrupt audio buffer, a bad STT model
    # state) would kill the task silently: the user says a command
    # after the wake word and JARVIS just never responds, with only an
    # easy-to-miss "Task exception was never retrieved" line on stderr.
    try:
        await self._handle_fast_path_utterance_inner(audio)
    except Exception as e:
        print(f"[FastPath] Utterance handling failed: {e}")
        traceback.print_exc()
        self.ui.write_log(f"ERR: Fast Path could not process that: {e}")
        self.set_speaking(False)

async def handle_fast_path_utterance_inner(self, audio: np.ndarray) -> None:
    if audio.size == 0:
        return
    loop = asyncio.get_event_loop()
    t_start = time.monotonic()

    text = await loop.run_in_executor(None, self._voice_pipeline.transcribe, audio)
    if not text:
        self.ui.write_log("SYS: Fast Path — heard nothing usable.")
        return

    self.ui.write_log(f"You: {text}")
    result = await loop.run_in_executor(None, fast_route, text)

    # Regex missed — try the MiniLM fuzzy-intent tier before giving up
    # on the Fast Path entirely (priority ladder: exact > regex >
    # classifier > LLM). Zero-arg intents only; see intent_classifier.py
    # for why parameterized commands are deliberately excluded here.
    if not result.matched and self._intent_classifier.is_ready:
        intent_result = await loop.run_in_executor(None, self._intent_classifier.classify, text)
        if intent_result.matched:
            handler = FAST_PATH_INTENT_DISPATCH.get(intent_result.intent)
            if handler is not None:
                try:
                    message = await loop.run_in_executor(None, handler, None)
                    result = RouteResult(
                        matched=True, rule_name=f"minilm:{intent_result.intent}",
                        success=True, message=message,
                    )
                    self.ui.write_log(
                        f"SYS: Fast Path fuzzy match '{intent_result.intent}' "
                        f"(confidence {intent_result.confidence:.2f})"
                    )
                except Exception as e:
                    result = RouteResult(
                        matched=True, rule_name=f"minilm:{intent_result.intent}",
                        success=False, message=f"Fast path '{intent_result.intent}' failed: {e}",
                    )

    if result.matched:
        latency.record("fast_path_total", (time.monotonic() - t_start) * 1000)
        self.ui.write_log(f"Jarvis: {result.message}")
        self.set_speaking(True)
        try:
            await loop.run_in_executor(None, self._voice_pipeline.tts.speak, result.message)
        except Exception as e:
            print(f"[FastPath] TTS error: {e}")
        finally:
            self.set_speaking(False)
        return

    # No fast rule matched — hand the ALREADY-TRANSCRIBED text to the
    # existing Smart Path exactly like a typed/dashboard/Telegram command
    # (same _on_text_command Gemini already uses for those), so Gemini's
    # reasoning and its own audio-out TTS handle it unchanged.
    latency.record("fast_path_passthrough", (time.monotonic() - t_start) * 1000)
    self._on_text_command(text)
