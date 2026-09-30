"""F10 sleep/wake toggle + auto-sleep after N minutes of silence (desktop
console mode only).

Mutes the agent's own audio input via `AgentSession.input.set_audio_enabled()`
either on demand (F10 while awake) or after a period with no speech from
either side. While asleep, F10 or the spoken wake word (wake_word.py,
"Hey Jarvis", local model) wakes it again.

Only meaningful for `worker.py console` (the desktop app): a global OS
hotkey has no clear owner when the worker runs as a cloud/dev job a browser
connects to, so `worker.py` only starts this when running in console mode.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import keyboard

import config
import hud_bridge
import mic_control
from tools import runtime
from wake_word import WakeWordListener

logger = logging.getLogger("jarvis-voice-agent.sleep_wake")

CHECK_INTERVAL_S = 5.0
MIC_POLL_S = 0.3


class SleepWakeController:
    def __init__(self, session: Any, hud_lines: list[dict[str, str]]) -> None:
        self._session = session
        self._hud_lines = hud_lines
        self._last_activity = time.time()
        self._asleep = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._mic_task: asyncio.Task[None] | None = None
        self._muted = False
        self._wake_word = WakeWordListener(self._on_wake_word)

    @property
    def asleep(self) -> bool:
        return self._asleep

    @property
    def muted(self) -> bool:
        return self._muted

    def note_activity(self) -> None:
        self._last_activity = time.time()

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._monitor_task = asyncio.create_task(self._monitor_loop(), name="sleep_wake_monitor")
        mic_control.set_muted(False)          # a fresh start always listens
        self._mic_task = asyncio.create_task(self._mic_loop(), name="mic_switch")
        keyboard.add_hotkey(config.WAKE_HOTKEY, self._on_hotkey)
        if config.MIC_HOTKEY and config.MIC_HOTKEY.lower() != config.WAKE_HOTKEY.lower():
            keyboard.add_hotkey(config.MIC_HOTKEY, self._on_mic_hotkey)
        logger.info(
            "sleep/wake hotkey registered: %s (auto-sleep after %.0fs of silence)",
            config.WAKE_HOTKEY,
            config.SLEEP_AFTER_SILENCE_S,
        )

    def stop(self) -> None:
        self._wake_word.stop()
        for task in (self._monitor_task, self._mic_task):
            if task is not None:
                task.cancel()
        for key in (config.WAKE_HOTKEY, config.MIC_HOTKEY):
            try:
                keyboard.remove_hotkey(key)
            except (KeyError, ValueError):
                pass

    def _on_hotkey(self) -> None:
        # Runs on keyboard's own listener thread; hop back onto the asyncio loop.
        # Toggles: asleep -> wake, awake -> sleep immediately (not just a
        # reset of the silence timer).
        # A muted mic comes back on first (F10 is the keyboard way out of mute).
        if self._loop is not None:
            if self._muted:
                mic_control.set_muted(False)      # _mic_loop picks it up and wakes if needed
                return
            coro = self._wake() if self._asleep else self._sleep()
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _on_mic_hotkey(self) -> None:
        # Runs on keyboard's listener thread; _mic_loop does the actual switch and the announcement.
        mic_control.set_muted(not mic_control.is_muted())

    def _on_wake_word(self) -> None:
        # Runs on the wake-word thread; same hop as the hotkey, but only wakes.
        if self._loop is not None and self._asleep and not self._muted:
            asyncio.run_coroutine_threadsafe(self._wake(), self._loop)

    async def _monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(CHECK_INTERVAL_S)
            if not self._asleep and not self._muted and time.time() - self._last_activity > config.SLEEP_AFTER_SILENCE_S:
                await self._sleep()

    async def _mic_loop(self) -> None:
        """Follows mic_control's switch (panel button, voice, Telegram, F10)."""
        while True:
            await asyncio.sleep(MIC_POLL_S)
            try:
                muted = mic_control.is_muted()
                if muted == self._muted:
                    continue
                self._muted = muted
                if muted:
                    self._wake_word.stop()
                    self._session.input.set_audio_enabled(False)
                    await runtime.say("Микрофон выключен.")
                    logger.info("microphone muted")
                else:
                    logger.info("microphone unmuted")
                    if self._asleep:
                        await self._wake()
                    else:
                        self.note_activity()
                        self._session.input.set_audio_enabled(True)
                        await runtime.say("Микрофон включён.")
            except Exception:
                logger.exception("mic switch tick failed")

    async def _sleep(self) -> None:
        self._asleep = True
        how = f"Нажмите {config.WAKE_HOTKEY.upper()}"
        if self._wake_word.available:
            how += " или скажите «Hey Jarvis»"
        await runtime.say(f"Ухожу в спящий режим. {how}, чтобы разбудить.")
        self._session.input.set_audio_enabled(False)
        if not self._muted:
            self._wake_word.start()
        hud_bridge.write_state("sleeping", self._hud_lines)
        logger.info("entered sleep mode after %.0fs of silence", config.SLEEP_AFTER_SILENCE_S)

    async def _wake(self) -> None:
        self.note_activity()
        if not self._asleep:
            return
        self._asleep = False
        self._wake_word.stop()
        self._session.input.set_audio_enabled(True)
        hud_bridge.write_state("listening", self._hud_lines)
        await runtime.say("Слушаю.")
        logger.info("woke up")
