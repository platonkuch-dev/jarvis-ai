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

import config
import hud_bridge
import memory_trim
import mic_control
from global_hotkeys import GlobalHotkeys
from tools import runtime
from wake_word import WakeWordListener

logger = logging.getLogger("jarvis-voice-agent.sleep_wake")

CHECK_INTERVAL_S = 5.0
MIC_POLL_S = 0.3
TRIM_DELAY_S = 8.0        # let the goodbye line finish playing first
RETRIM_EVERY_S = 900.0    # background work (Telegram, monitors) slowly pulls pages back in


class SleepWakeController:
    def __init__(self, session: Any, hud_lines: list[dict[str, str]]) -> None:
        self._session = session
        self._hud_lines = hud_lines
        self._last_activity = time.time()
        self._asleep = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._mic_task: asyncio.Task[None] | None = None
        self._trim_task: asyncio.Task[None] | None = None
        self._muted = False
        self._wake_word = WakeWordListener(self._on_wake_word)
        self._hotkeys = GlobalHotkeys()

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
        self._hotkeys.add(config.WAKE_HOTKEY, self._on_hotkey)
        if config.MIC_HOTKEY and config.MIC_HOTKEY.lower() != config.WAKE_HOTKEY.lower():
            self._hotkeys.add(config.MIC_HOTKEY, self._on_mic_hotkey)
        self._hotkeys.start()
        logger.info(
            "sleep/wake hotkey registered: %s (auto-sleep after %.0fs of silence)",
            config.WAKE_HOTKEY,
            config.SLEEP_AFTER_SILENCE_S,
        )

    def stop(self) -> None:
        self._wake_word.stop()
        for task in (self._monitor_task, self._mic_task, self._trim_task):
            if task is not None:
                task.cancel()
        self._hotkeys.stop()

    def _on_hotkey(self) -> None:
        # Runs on the hotkey thread (global_hotkeys.py); hop back onto the asyncio loop.
        # Toggles: asleep -> wake, awake -> sleep immediately (not just a
        # reset of the silence timer).
        # A muted mic comes back on first (F10 is the keyboard way out of mute).
        if self._loop is not None:
            if self._muted:
                mic_control.set_muted(False)      # _mic_loop picks it up and wakes if needed
                return
            # F10 while awake cuts him off mid-word and drops straight into sleep, silently.
            coro = self._wake() if self._asleep else self._sleep(announce=False, interrupt=True)
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _on_mic_hotkey(self) -> None:
        # Runs on the hotkey thread; _mic_loop does the actual switch and the announcement.
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
                # Off the loop: a file read stalled by antivirus/disk spin-up
                # blocked audio for up to 2 s here.
                muted = await asyncio.to_thread(mic_control.is_muted)
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

    async def _sleep(self, announce: bool = True, interrupt: bool = False) -> None:
        if self._asleep:
            return
        self._asleep = True
        if interrupt:
            # Mic off first so nothing he hears re-triggers a turn, then cut the speech
            # (and the reply still being generated) -- queued lines included.
            self._session.input.set_audio_enabled(False)
            hud_bridge.write_state("sleeping", self._hud_lines)
            try:
                self._session.interrupt(force=True)
            except Exception:
                logger.debug("nothing to interrupt", exc_info=True)
        if announce:
            how = f"Нажмите {config.WAKE_HOTKEY.upper()}"
            if self._wake_word.available:
                how += " или скажите «Hey Jarvis»"
            await runtime.say(f"Ухожу в спящий режим. {how}, чтобы разбудить.")
        self._session.input.set_audio_enabled(False)
        if not self._muted:
            self._wake_word.start()
        hud_bridge.write_state("sleeping", self._hud_lines)
        logger.info("entered sleep mode (%s)", "hotkey" if interrupt else "silence")
        if self._trim_task is None or self._trim_task.done():
            self._trim_task = asyncio.create_task(self._sleep_lean(), name="sleep_lean")

    async def _sleep_lean(self) -> None:
        """Asleep, Jarvis shouldn't sit on RAM: stop the idle claude process
        and hand every Jarvis process's untouched pages back to Windows,
        again every RETRIM_EVERY_S while he stays asleep."""
        await asyncio.sleep(TRIM_DELAY_S)
        while self._asleep:
            suspend = getattr(getattr(self._session, "llm", None), "suspend", None)
            if suspend is not None:
                try:
                    await suspend()
                except Exception:
                    logger.exception("could not stop the LLM process for sleep")
            await asyncio.to_thread(memory_trim.trim_all)
            await asyncio.sleep(RETRIM_EVERY_S)

    async def _wake(self) -> None:
        self.note_activity()
        if not self._asleep:
            return
        self._asleep = False
        self._wake_word.stop()
        if self._trim_task is not None:
            self._trim_task.cancel()
        resume = getattr(getattr(self._session, "llm", None), "resume", None)
        if resume is not None:
            asyncio.create_task(resume(), name="llm_resume")
        self._session.input.set_audio_enabled(True)
        hud_bridge.write_state("listening", self._hud_lines)
        await runtime.say("Слушаю.")
        logger.info("woke up")
