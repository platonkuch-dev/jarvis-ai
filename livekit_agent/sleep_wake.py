"""F10 sleep/wake toggle + auto-sleep after N minutes of silence (desktop
console mode only).

Mutes the agent's own audio input via `AgentSession.input.set_audio_enabled()`
either on demand (F10 while awake) or after a period with no speech from
either side -- a push-to-sleep/wake substitute for a spoken wake word, since
the agent otherwise listens continuously.

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
from tools import runtime

logger = logging.getLogger("jarvis-voice-agent.sleep_wake")

CHECK_INTERVAL_S = 5.0


class SleepWakeController:
    def __init__(self, session: Any, hud_lines: list[dict[str, str]]) -> None:
        self._session = session
        self._hud_lines = hud_lines
        self._last_activity = time.time()
        self._asleep = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._monitor_task: asyncio.Task[None] | None = None

    @property
    def asleep(self) -> bool:
        return self._asleep

    def note_activity(self) -> None:
        self._last_activity = time.time()

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._monitor_task = asyncio.create_task(self._monitor_loop(), name="sleep_wake_monitor")
        keyboard.add_hotkey(config.WAKE_HOTKEY, self._on_hotkey)
        logger.info(
            "sleep/wake hotkey registered: %s (auto-sleep after %.0fs of silence)",
            config.WAKE_HOTKEY,
            config.SLEEP_AFTER_SILENCE_S,
        )

    def stop(self) -> None:
        if self._monitor_task is not None:
            self._monitor_task.cancel()
        try:
            keyboard.remove_hotkey(config.WAKE_HOTKEY)
        except (KeyError, ValueError):
            pass

    def _on_hotkey(self) -> None:
        # Runs on keyboard's own listener thread; hop back onto the asyncio loop.
        # Toggles: asleep -> wake, awake -> sleep immediately (not just a
        # reset of the silence timer).
        if self._loop is not None:
            coro = self._wake() if self._asleep else self._sleep()
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(CHECK_INTERVAL_S)
            if not self._asleep and time.time() - self._last_activity > config.SLEEP_AFTER_SILENCE_S:
                await self._sleep()

    async def _sleep(self) -> None:
        self._asleep = True
        await runtime.say(
            f"Ухожу в спящий режим. Нажмите {config.WAKE_HOTKEY.upper()}, чтобы разбудить."
        )
        self._session.input.set_audio_enabled(False)
        hud_bridge.write_state("sleeping", self._hud_lines)
        logger.info("entered sleep mode after %.0fs of silence", config.SLEEP_AFTER_SILENCE_S)

    async def _wake(self) -> None:
        self.note_activity()
        if not self._asleep:
            return
        self._asleep = False
        self._session.input.set_audio_enabled(True)
        hud_bridge.write_state("listening", self._hud_lines)
        await runtime.say("Слушаю.")
        logger.info("woke up via %s", config.WAKE_HOTKEY)
