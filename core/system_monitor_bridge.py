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


async def run_system_monitor(self) -> None:
    """Background task: voice alerts when metrics exceed thresholds."""
    while True:
        await asyncio.sleep(10)
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
    """
    while True:
        await asyncio.sleep(60)   # evaluate once per minute

        if not self.session:
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
