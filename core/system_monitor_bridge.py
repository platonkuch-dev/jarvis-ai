"""
Orchestration loops for hardware alerts and proactive check-ins. The actual
logic lives in actions/system_monitor.py and actions/proactive.py -- these
are just the background tasks that poll them and talk to the Gemini session.

Split out of main.py (Stage 2 module split, see REWORK_PLAN.md) with no
behavior changes -- these were previously JarvisLive methods in main.py.
Each function takes the JarvisLive instance as `self`, exactly as when it
was a bound method; JarvisLive keeps thin wrapper methods of the same name
(minus the module qualifier) so every call site elsewhere is unchanged.
"""
from __future__ import annotations

import asyncio

from memory.memory_manager import load_memory
from core.assistant_state import get_state


async def run_system_monitor(self) -> None:
    """Background task: voice alerts when metrics exceed thresholds."""
    while True:
        await asyncio.sleep(5 if get_state()["power_mode"]["enabled"] else 10)
        alert = await asyncio.to_thread(self._sys_monitor.check)
        if alert and self.session:
            try:
                await self.session.send_client_content(
                    turns={"parts": [{"text": alert}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[Monitor] ⚠️ Could not send alert: {e}")

async def run_proactive_mode(self) -> None:
    """
    Background task: periodically checks if the user has been silent long enough,
    then hands time + memory context to Gemini so it can decide what (if anything)
    to say proactively. No hardcoded rules — Gemini makes the call.

    DISABLED as of the wake-word-gated session model (main.py's
    _require_wake_word, core/idle_watchdog.py): this only ever fires while
    self.session is set, but the idle watchdog now closes the session
    after ~150s of silence (core/idle_watchdog.py's IDLE_CLOSE_SECS) --
    far short of ProactiveEngine's 900s threshold (actions/proactive.py),
    which can now structurally never be reached. Left in place rather than
    deleted for a future rework that re-scopes the trigger to fire across
    sessions (track wall-clock time since the session last closed, and
    have this task open a session itself once idle long enough) --
    explicitly out of scope for now; the user chose to disable rather than
    rebuild this. Kept as a real early-return (not just dead code) so this
    task isn't spending a wakeup a minute for nothing on top of it.
    """
    return
    while True:
        await asyncio.sleep(60)   # evaluate once per minute

        if not self.session:
            continue

        if get_state()["focus"]["active"]:
            continue

        with self._speaking_lock:
            speaking = self._is_speaking
        if speaking:
            continue

        if not self._proactive.should_trigger(self._last_user_speech):
            continue

        self._proactive.mark_triggered()

        try:
            memory = await asyncio.to_thread(load_memory)
            prompt = self._proactive.build_prompt(memory)
            await self.session.send_client_content(
                turns={"parts": [{"text": prompt}]},
                turn_complete=True,
            )
            self.ui.write_log("SYS: Proactive check-in.")
        except Exception as e:
            print(f"[Proactive] ⚠️ {e}")
