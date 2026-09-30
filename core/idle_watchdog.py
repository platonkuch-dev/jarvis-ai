"""
Idle watchdog: closes the Gemini Live session after a period of user
silence, so the wake-word-gated app (see main.py's `_require_wake_word`)
shrinks back to its silent, ambient state instead of staying connected
indefinitely once a conversation goes quiet.

Split out as its own module (matching the one-function-per-concern shape
of core/dashboard_bridge.py, core/telegram_bridge.py, core/routine_bridge.py)
rather than folded into core/system_monitor_bridge.py, so it stays
independently testable and doesn't get tangled with the 15-minute
proactive check-in logic it deliberately supersedes for now (see
actions/proactive.py's module docstring).
"""
from __future__ import annotations

import asyncio
import time


class IntentionalIdleClose(Exception):
    """Raised inside the connected TaskGroup to cleanly unwind it after a
    genuine period of user silence -- not an error. main.py's run()
    classifies this before its normal audio/API-key/network error
    branches and skips the backoff/error UI those apply."""


IDLE_CLOSE_SECS = 60  # 1 min of silence before shrinking back to ambient/idle.

_POLL_SECS = 10  # short enough that a 60s threshold doesn't overshoot by much


async def run_idle_watchdog(self) -> None:
    while True:
        await asyncio.sleep(_POLL_SECS)
        if self.session and time.monotonic() - self._last_user_speech >= IDLE_CLOSE_SECS:
            raise IntentionalIdleClose()
