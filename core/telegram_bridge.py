"""
Telegram userbot command relay: forwards text commands queued by
integrations/telegram_userbot.py into the Gemini Live session.

Split out of main.py (Stage 2 module split, see REWORK_PLAN.md) with no
behavior changes -- this was previously a JarvisLive method in main.py.
Takes the JarvisLive instance as `self`, exactly as when it was a bound
method; JarvisLive keeps a thin wrapper method of the same name (minus the
module qualifier) so every call site elsewhere is unchanged.
"""
from __future__ import annotations

import asyncio


async def process_telegram_commands(self) -> None:
    if not self._telegram:
        return
    while True:
        try:
            text, chat_id = await asyncio.wait_for(
                self._telegram._command_queue.get(), timeout=0.5
            )
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            print(f"[Telegram] Command error: {e}")
            await asyncio.sleep(0.5)
            continue

        if not text:
            continue
        try:
            # Set the reply target before waking/waiting -- if this
            # message ends up sent as the new session's opener (below),
            # Gemini's reply needs somewhere to route back to as soon as
            # the session connects.
            self._telegram_reply_target = chat_id
            self._telegram_audio_chunks = []
            # main.py's run() only connects once woken (see
            # _require_wake_word) -- a Telegram command arriving while
            # idle IS a wake trigger, same as voice/typed/dashboard input.
            # If this consumed `text` as the new session's opener, run()
            # sends it once connected -- don't send it again below.
            sent_as_opener = self._trigger_wake(text)
            # Wait up to 8s for session to become ready after a wake
            for _ in range(80):
                if self.session:
                    break
                await asyncio.sleep(0.1)
            if sent_as_opener:
                if self.session:
                    self.ui.write_log(f"[Telegram]: {text}")
                else:
                    print(f"[Telegram] Wake never connected: {text}")
            elif self.session:
                await self.session.send_client_content(
                    turns={"parts": [{"text": text}]},
                    turn_complete=True,
                )
                self.ui.write_log(f"[Telegram]: {text}")
            else:
                print(f"[Telegram] Dropped command (no session): {text}")
        except Exception as e:
            # Unlike the queue.get() try/except above, this used to be
            # unguarded — a single send_client_content() failure (e.g.
            # session torn down mid-send during a reconnect) killed this
            # whole background task permanently, silently dropping every
            # Telegram command for the rest of the process's life.
            # _process_dashboard_commands() already guards its equivalent
            # send; mirror that here so one bad send can't end the relay.
            print(f"[Telegram] Failed to relay command to Gemini: {e}")
