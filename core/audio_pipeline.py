"""
Audio I/O pipeline for JarvisLive: mic capture -> Gemini Live realtime send,
and Gemini Live audio deltas -> speaker playback + response transcript/tool
handling.

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
from datetime import datetime

import sounddevice as sd

from core.session import clean_transcript as _clean_transcript

CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024


async def send_realtime(self):
    while True:
        msg = await self.out_queue.get()
        await self.session.send_realtime_input(media=msg)

async def listen_audio(self):
    print("[JARVIS] 🎤 Mic started")
    loop = asyncio.get_event_loop()

    def callback(indata, frames, time_info, status):
        with self._speaking_lock:
            jarvis_speaking = self._is_speaking
        with self._fast_path_lock:
            fast_path_recording = self._fast_path_state == "recording"
        # Normally the mic is gated shut while JARVIS is speaking, so it can never
        # hear its own voice from the speakers. With barge-in enabled we keep the
        # mic open through JARVIS's speech too — the user is expected to be on
        # headphones, so there's no echo, and Gemini's server-side VAD (the
        # default START_OF_ACTIVITY_INTERRUPTS behavior) handles the actual cutoff
        # once it detects genuine new speech; see the `interrupted` handling below.
        # Also gated while the Fast Path is actively recording a locally-handled
        # utterance — otherwise Gemini would hear the same command and potentially
        # execute it a second time via its own function-calling.
        gated = (jarvis_speaking and not self.ui.barge_in_enabled) or fast_path_recording
        if not gated and not self.ui.muted and not self._phone_active:
            data = indata.tobytes()
            loop.call_soon_threadsafe(
                self.out_queue.put_nowait,
                {"data": data, "mime_type": "audio/pcm"}
            )
        # Fast Path always gets a copy (independent of Gemini's own gating)
        # so the wake word can fire even while JARVIS/Gemini is mid-turn.
        if not self.ui.muted and not self._phone_active and self._fast_path_queue is not None:
            loop.call_soon_threadsafe(self._fast_path_queue.put_nowait, indata.copy())

    try:
        with sd.InputStream(
            samplerate=SEND_SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=CHUNK_SIZE,
            callback=callback,
        ):
            print("[JARVIS] 🎤 Mic stream open")
            while True:
                await asyncio.sleep(0.1)
    except Exception as e:
        print(f"[JARVIS] ❌ Mic: {e}")
        raise

async def receive_audio(self):
    print("[JARVIS] 👂 Recv started")
    out_buf, in_buf = [], []

    try:
        while True:
            async for response in self.session.receive():

                if response.data:
                    if self._interrupted:
                        pass  # discard: interrupted
                    else:
                        if self._turn_done_event and self._turn_done_event.is_set():
                            self._turn_done_event.clear()
                        # Split into ~50 ms chunks so interrupt() stops audio within 50 ms
                        # (24000 Hz × 2 bytes/sample × 0.05 s = 2400 bytes per slice)
                        _audio_data = response.data
                        _SLICE = 2400
                        for _i in range(0, len(_audio_data), _SLICE):
                            self.audio_in_queue.put_nowait(_audio_data[_i : _i + _SLICE])
                        if self._telegram and self._telegram_reply_target is not None:
                            self._telegram_audio_chunks.append(_audio_data)

                if response.server_content:
                    sc = response.server_content

                    # Voice barge-in: the server detected genuine new speech while
                    # JARVIS was talking (its default START_OF_ACTIVITY_INTERRUPTS
                    # behavior) and cut its own response short. Mirror that locally
                    # exactly like a manual Esc/Interrupt press.
                    if sc.interrupted and not self._interrupted:
                        print("[JARVIS] 🎙️ Voice barge-in — server interrupted response")
                        self.interrupt()

                    if sc.output_transcription and sc.output_transcription.text:
                        txt = _clean_transcript(sc.output_transcription.text)
                        if txt and txt != (out_buf[-1] if out_buf else ""):
                            out_buf.append(txt)

                    if sc.input_transcription and sc.input_transcription.text:
                        txt = _clean_transcript(sc.input_transcription.text)
                        if txt:
                            in_buf.append(txt)
                            self._last_user_speech = time.monotonic()

                    if sc.turn_complete:
                        if self._turn_done_event:
                            self._turn_done_event.set()

                        # If this turn_complete ends an interrupted response, clear the
                        # flag and skip all further processing for that turn.
                        if self._interrupted:
                            self._interrupted = False
                            in_buf  = []
                            out_buf = []
                            continue

                        full_in = " ".join(in_buf).strip()
                        if full_in:
                            self.ui.write_log(f"You: {full_in}")
                            if self._dashboard:
                                asyncio.create_task(self._dashboard.broadcast({
                                    "type": "log", "speaker": "user",
                                    "text": full_in,
                                    "ts": datetime.now().isoformat(),
                                }))
                        in_buf = []

                        full_out = " ".join(out_buf).strip()

                        _telegram_target = None
                        if self._telegram and self._telegram_reply_target is not None:
                            _telegram_target = self._telegram_reply_target
                            self._telegram_reply_target = None

                        if full_out:
                            self.ui.write_log(f"Jarvis: {full_out}")
                            if self._dashboard:
                                asyncio.create_task(self._dashboard.broadcast({
                                    "type": "log", "speaker": "jarvis",
                                    "text": full_out,
                                    "ts": datetime.now().isoformat(),
                                }))
                            if _telegram_target is not None:
                                asyncio.create_task(self._telegram.send_reply(_telegram_target, full_out))
                        out_buf = []

                        if _telegram_target is not None:
                            pcm = b"".join(self._telegram_audio_chunks)
                            self._telegram_audio_chunks = []
                            if pcm and self._telegram.should_reply_with_voice(_telegram_target):
                                asyncio.create_task(self._telegram.send_voice(_telegram_target, pcm))

                        # Vision injection: model finished tool-response turn → now send the image
                        if self._pending_vision and self.session:
                            import base64 as _b64
                            img_b, mime_t, question, angle = self._pending_vision
                            self._pending_vision = None
                            b64 = _b64.b64encode(img_b).decode("ascii")
                            print(f"[Vision] 📤 {len(img_b):,} bytes (angle={angle}) → main session")
                            await self.session.send_client_content(
                                turns={"parts": [
                                    {"inline_data": {"mime_type": mime_t, "data": b64}},
                                    {"text": question},
                                ]},
                                turn_complete=True,
                            )
                            # Mark next turn_complete behaviour depending on angle
                            if self._vision_cam_active:
                                # Camera: keep busy until JARVIS finishes speaking the answer
                                self._vision_cam_active    = False
                                self._vision_close_pending = True
                            else:
                                # Screen-only: no camera to close; release busy flag now
                                self._vision_busy = False
                        elif self._vision_close_pending:
                            # This turn_complete IS the vision answer — close camera + release busy flag
                            self._vision_close_pending = False
                            self._vision_busy = False
                            async def _cam_close():
                                await asyncio.sleep(2.0)
                                # Fire-and-forget task (see actions/screen_processor.py's
                                # own _deferred_close for the same pattern) — swallow so a
                                # UI-teardown glitch doesn't surface as an unretrieved
                                # task exception with no user-visible symptom either way.
                                try:
                                    self.ui.stop_camera_stream()
                                except Exception as e:
                                    print(f"[Vision] Camera stream stop failed: {e}")
                            asyncio.create_task(_cam_close())

                if response.tool_call:
                    fn_responses = []
                    for fc in response.tool_call.function_calls:
                        print(f"[JARVIS] 📞 {fc.name}")
                        fr = await self._execute_tool(fc)
                        fn_responses.append(fr)
                    await self.session.send_tool_response(
                        function_responses=fn_responses
                    )
    except Exception as e:
        print(f"[JARVIS] ❌ Recv: {e}")
        traceback.print_exc()
        raise

async def play_audio(self):
    print("[JARVIS] 🔊 Play started")

    stream = sd.RawOutputStream(
        samplerate=RECEIVE_SAMPLE_RATE,
        channels=CHANNELS,
        dtype="int16",
        blocksize=CHUNK_SIZE,
    )
    stream.start()

    try:
        while True:
            try:
                chunk = await asyncio.wait_for(
                    self.audio_in_queue.get(),
                    timeout=0.1
                )
            except asyncio.TimeoutError:
                if (
                    self._turn_done_event
                    and self._turn_done_event.is_set()
                    and self.audio_in_queue.empty()
                ):
                    self.set_speaking(False)
                    self._turn_done_event.clear()
                continue
            self.set_speaking(True)
            try:
                await asyncio.to_thread(stream.write, chunk)
            except (RuntimeError, asyncio.CancelledError):
                break   # executor shutting down — exit cleanly
    except Exception as e:
        print(f"[JARVIS] ❌ Play: {e}")
        raise
    finally:
        self.set_speaking(False)
        stream.stop()
        stream.close()
