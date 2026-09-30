"""
Fast Path: local wake word / STT / router / TTS, bypassing Gemini Live
entirely for a small set of zero-latency commands.

Wake-word detection is Whisper-based, not a dedicated classifier model:
openWakeWord's pretrained "hey_jarvis" (voice/wake_word.py) can't recognize
Russian pronunciation at all (measured near-zero confidence against
synthesized "Джарвис" clips -- see the conversation this replaced it in).
Instead: any speech onset (voice/vad.py's SpeechOnsetDetector, same Silero
VAD engine already bundled) starts a normal VAD-gated recording, exactly
like the old post-wake-word command recording did; once it ends, the whole
thing is transcribed with faster-whisper (already loaded for everything
else Fast Path does) and checked for "джарвис" in the text itself. This
correctly handles any language faster-whisper handles, at the cost of
transcribing every utterance near the mic instead of only ones a cheap
classifier flagged first -- an explicit, accepted tradeoff, not an oversight.

Two recording flavors share the same VAD-gated accumulate-until-silence
mechanics (core/fast_path.py's own `_record_until_silence` helper) but
differ in what happens once they finish:
  - "listening_for_wake": the FIRST utterance since idle. Checked for
    "джарвис" once transcribed. Not found -> discarded, back to idle
    (nothing shown, nothing logged for the user -- ambient noise/speech
    isn't Jarvis's business). Found -> whatever follows "джарвис" in that
    SAME utterance (if the user said it all in one breath, e.g. "джарвис
    включи свет") is dispatched immediately as the command; if nothing
    meaningful follows, moves to...
  - "listening_for_command": a second recording, no wake-word check needed
    (already confirmed) -- whatever gets transcribed here is dispatched
    directly, exactly like every Fast Path utterance always was.

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

WAKE_PHRASE = "джарвис"


def _split_after_wake_phrase(text: str) -> tuple[bool, str]:
    """(found, remainder) -- remainder is whatever follows the LAST
    occurrence of the wake phrase, stripped of leading punctuation/space
    (e.g. "Джарвис, включи свет" -> (True, "включи свет"); "Джарвис" alone
    -> (True, ""); "смотрю сериал" -> (False, ""))."""
    lower = text.lower()
    idx = lower.rfind(WAKE_PHRASE)
    if idx == -1:
        return False, ""
    remainder = text[idx + len(WAKE_PHRASE):].strip(" ,.!?—-\n\t")
    return True, remainder


def trigger_manual_listen(self) -> None:
    """Skip wake-word detection entirely and start listening for a command
    right away — called when the user presses the global hotkey (see
    core/hotkey.py) instead of saying "джарвис". Loop-thread only; the
    hotkey fires on its own background thread, so main.py's wrapper hops
    over via self._loop.call_soon_threadsafe(...), exactly like
    _trigger_wake() does for other off-loop triggers.

    No-op if Fast Path isn't warmed up yet, or a wake/recording is already
    in progress (state != "idle") -- a hotkey press mid-command shouldn't
    reset an already-listening utterance out from under the user."""
    if not self._voice_pipeline.is_ready:
        return
    with self._fast_path_lock:
        if self._fast_path_state != "idle":
            return
        self._fast_path_state = "listening_for_command"
    # Same setup _handle_fast_path_wake_utterance_inner() does once it
    # finds "джарвис" and moves to "listening_for_command" -- barge-in
    # stop, fresh VAD utterance, visible feedback that Jarvis is listening.
    self._voice_pipeline.tts.stop()
    self._voice_pipeline.speech_onset.reset()
    self._voice_pipeline.vad.start_utterance()
    self._last_user_speech = time.monotonic()
    self.ui.write_log("SYS: Fast Path — hotkey pressed, listening...")
    self.ui.show_compact_bar()


async def run_fast_path(self) -> None:
    """
    Consumes the raw audio the mic callback fans out, independent of
    whatever Gemini is doing. Re-slices arbitrary-sized input blocks into
    the fixed 1280-sample (80ms) frames Silero VAD requires, runs speech-
    onset detection on every frame while idle, and once triggered, records
    until real trailing silence (not a fixed sleep) before handing the
    utterance off for transcription -- either checked for the wake phrase,
    or (once already confirmed) dispatched directly.
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
                    onset = await asyncio.to_thread(self._voice_pipeline.speech_onset.check, frame)
                except Exception as e:
                    print(f"[FastPath] Speech onset error: {e}")
                    continue
                if onset:
                    # Barge-in for local TTS: stop() is a cheap no-op if
                    # nothing is currently playing.
                    self._voice_pipeline.tts.stop()
                    self._voice_pipeline.speech_onset.reset()
                    self._voice_pipeline.vad.start_utterance()
                    recording_frames = []
                    with self._fast_path_lock:
                        self._fast_path_state = "listening_for_wake"
                    # Deliberately silent/invisible here -- onset alone isn't
                    # confirmation this was directed at Jarvis (could be
                    # ambient speech/TV/other people). The bar only appears
                    # once "джарвис" is actually found in the transcription
                    # below.

            elif state in ("listening_for_wake", "listening_for_command"):
                recording_frames.append(frame)
                try:
                    done = await asyncio.to_thread(self._voice_pipeline.vad.feed, frame, frame_ms)
                except Exception as e:
                    print(f"[FastPath] VAD error: {e}")
                    done = True
                if done:
                    audio = np.concatenate(recording_frames) if recording_frames else np.zeros(0, dtype=np.int16)
                    recording_frames = []
                    if state == "listening_for_wake":
                        asyncio.create_task(self._handle_fast_path_wake_utterance(audio))
                    else:
                        with self._fast_path_lock:
                            self._fast_path_state = "idle"
                        asyncio.create_task(self._handle_fast_path_utterance(audio))


async def handle_fast_path_wake_utterance(self, audio: np.ndarray) -> None:
    """The first recording since idle -- transcribe and look for the wake
    phrase before deciding what (if anything) happens next. Always resets
    _fast_path_state back to "idle" or forward to "listening_for_command"
    before returning, so run_fast_path()'s loop is never left stuck."""
    try:
        await _handle_fast_path_wake_utterance_inner(self, audio)
    except Exception as e:
        print(f"[FastPath] Wake utterance handling failed: {e}")
        traceback.print_exc()
        self.ui.write_log(f"ERR: Fast Path could not process that: {e}")
        self.set_speaking(False)
        with self._fast_path_lock:
            self._fast_path_state = "idle"


async def _handle_fast_path_wake_utterance_inner(self, audio: np.ndarray) -> None:
    if audio.size == 0:
        with self._fast_path_lock:
            self._fast_path_state = "idle"
        return

    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, self._voice_pipeline.transcribe, audio)

    found, remainder = _split_after_wake_phrase(text) if text else (False, "")
    if not found:
        # Ambient speech, not directed at Jarvis -- discard quietly.
        with self._fast_path_lock:
            self._fast_path_state = "idle"
        return

    self._last_user_speech = time.monotonic()
    self.ui.write_log("SYS: Fast Path — wake word detected, listening...")
    # Instant visual feedback that the wake word fired. Hidden again in
    # handle_local_text() if this turns out to be a pure local command (no
    # Gemini connection needed) -- otherwise it stays up, now driven by the
    # connected session's own state transitions.
    self.ui.show_compact_bar()
    latency.record("fast_path_wake_word", 0.0)

    if remainder:
        # Said in one breath, e.g. "джарвис включи свет" -- dispatch
        # immediately, no second recording needed.
        await _dispatch_fast_path_text(self, remainder)
        with self._fast_path_lock:
            self._fast_path_state = "idle"
        return

    # Bare wake phrase -- wait for the actual command as a fresh utterance.
    self._voice_pipeline.vad.start_utterance()
    with self._fast_path_lock:
        self._fast_path_state = "listening_for_command"


async def handle_fast_path_utterance(self, audio: np.ndarray) -> None:
    # This method only ever runs as a bare asyncio.create_task(...) (see
    # run_fast_path() above) — nothing awaits it or reads its result.
    # Before this try/except, an exception from transcribe() or
    # fast_route() (e.g. a corrupt audio buffer, a bad STT model state)
    # would kill the task silently: the user says a command after the
    # wake word and JARVIS just never responds, with only an easy-to-miss
    # "Task exception was never retrieved" line on stderr.
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

    # core/idle_watchdog.py's idle-close clock only used to reset from
    # Gemini-side transcription (core/audio_pipeline.py) -- without this,
    # a user issuing only local Fast Path commands post-connection would
    # never refresh it, and the watchdog could close an actively-used
    # session out from under them.
    self._last_user_speech = time.monotonic()

    self.ui.write_log(f"You: {text}")
    await _dispatch_fast_path_text(self, text, t_start=t_start)


async def _dispatch_fast_path_text(self, text: str, t_start: float | None = None) -> None:
    """Shared tail end for both recording flavors: try the local router
    first, fall back to the existing Smart Path with already-transcribed
    text. `t_start` is only used for latency bookkeeping when the caller
    has one (handle_fast_path_utterance_inner does; the wake-phrase-in-
    one-breath path in _handle_fast_path_wake_utterance_inner doesn't, and
    that's fine -- fast_path_wake_word already recorded its own latency
    point)."""
    handled = await handle_local_text(self, text)
    if handled:
        if t_start is not None:
            latency.record("fast_path_total", (time.monotonic() - t_start) * 1000)
        return

    # No local rule matched — hand the ALREADY-TRANSCRIBED text to the
    # existing Smart Path exactly like a typed/dashboard/Telegram command.
    if t_start is not None:
        latency.record("fast_path_passthrough", (time.monotonic() - t_start) * 1000)
    self._on_text_command(text)


async def handle_local_text(self, text: str) -> bool:
    """Execute a known local command and speak its result without cloud access."""
    loop = asyncio.get_event_loop()
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
        self.ui.write_log(f"Jarvis: {result.message}")
        self.set_speaking(True)
        try:
            await loop.run_in_executor(None, self._voice_pipeline.tts.speak, result.message)
        except Exception as e:
            print(f"[FastPath] TTS error: {e}")
        finally:
            self.set_speaking(False)
            # Pure local command, handled entirely offline -- nothing
            # opened a Gemini connection (still gated), so the compact bar
            # shown on wake-word detection has no ongoing conversation to
            # reflect. Hide it back down to the ambient/silent state.
            if self._require_wake_word:
                self.ui.hide_compact_bar()
        return True
    return False
