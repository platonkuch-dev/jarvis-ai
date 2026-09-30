"""
Web dashboard bridge: phone-mic audio relay and text-command relay from the
dashboard's websocket queues into the Gemini Live session.

Split out of main.py (Stage 2 module split, see REWORK_PLAN.md) with no
behavior changes -- these were previously JarvisLive methods in main.py.
Each function takes the JarvisLive instance as `self`, exactly as when it
was a bound method; JarvisLive keeps thin wrapper methods of the same name
(minus the module qualifier) so every call site elsewhere is unchanged.
"""
from __future__ import annotations

import asyncio


async def relay_phone_audio(self) -> None:
    """Forward phone mic PCM chunks from dashboard queue into the Gemini Live session."""
    q = self._dashboard._phone_audio_queue
    while True:
        try:
            chunk = await asyncio.wait_for(q.get(), timeout=1.0)
        except asyncio.TimeoutError:
            # No audio for 1 s → phone mic inactive, give PC mic back
            self._phone_active = False
            continue
        self._phone_active = True   # phone is streaming — silence PC mic
        with self._speaking_lock:
            speaking = self._is_speaking
        if not speaking and not self.ui.muted:
            try:
                self.out_queue.put_nowait(chunk)
            except asyncio.QueueFull:
                pass

def on_phone_connected(self) -> None:
    self.ui.write_log("SYS: Phone connected via Remote Dashboard.")
    self.ui.notify_phone_connected()

async def process_dashboard_commands(self) -> None:
    while True:
        try:
            text = await asyncio.wait_for(
                self._dashboard._command_queue.get(), timeout=0.5
            )
            if not text:
                continue
            # main.py's run() only connects once woken (see
            # _require_wake_word) -- a dashboard command arriving while
            # idle IS a wake trigger, same as voice/typed/Telegram input,
            # so it actually has a session to wait for below instead of
            # just hoping one appears. If this consumed `text` as the new
            # session's opener, run() sends it once connected -- don't
            # send it again below.
            sent_as_opener = self._trigger_wake(text)
            # Wait up to 8s for session to become ready after a wake
            for _ in range(80):
                if self.session:
                    break
                await asyncio.sleep(0.1)
            if sent_as_opener:
                if self.session:
                    self.ui.write_log(f"[Web]: {text}")
                else:
                    print(f"[Dashboard] Wake never connected: {text}")
            elif self.session:
                await self.session.send_client_content(
                    turns={"parts": [{"text": text}]},
                    turn_complete=True,
                )
                self.ui.write_log(f"[Web]: {text}")
            else:
                print(f"[Dashboard] Dropped command (no session): {text}")
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            print(f"[Dashboard] Command error: {e}")
            await asyncio.sleep(0.5)
