"""Runs persisted daily JARVIS routines while the assistant is online."""
from __future__ import annotations

import asyncio
from datetime import datetime

from core.assistant_state import now_iso, update_state


async def run_scheduled_routines(self) -> None:
    while True:
        await asyncio.sleep(20)
        if not self.session:
            continue
        now = datetime.now()
        current_time = now.strftime("%H:%M")
        current_day = now.strftime("%Y-%m-%d")
        due: list[dict] = []

        def collect_due(state: dict) -> None:
            for routine in state["routines"]:
                if routine.get("schedule") != current_time or routine.get("last_run") == current_day:
                    continue
                routine["last_run"] = current_day
                routine["last_run_at"] = now_iso()
                due.append(dict(routine))

        update_state(collect_due)
        for routine in due:
            try:
                await self.session.send_client_content(
                    turns={"parts": [{"text": f"[SCHEDULED ROUTINE: {routine['name']}] {routine['instruction']}"}]},
                    turn_complete=True,
                )
                self.ui.write_log(f"SYS: Running scheduled routine: {routine['name']}")
            except Exception as error:
                print(f"[Routine] Could not run '{routine['name']}': {error}")