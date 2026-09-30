"""
Digital Ghost's background scan loop -- the piece that actually keeps
ghost/store.py's baseline current. Started once from main.py's run() (same
place dashboard/telegram get started) via GhostEngine().start(), which
spawns one daemon thread; nothing here touches asyncio, matching
ui/sys_metrics.py's own plain-thread pattern (the collectors block on
subprocess/registry calls, which would stall the event loop if run as
asyncio tasks).

Each cycle: run every state collector (processes/files/registry/services/
network/dns/devices/applications), diff-and-store each one, then pull any
new Windows Event Log entries since the last cycle. One collector's
exception never aborts the cycle for the others -- a transient PowerShell
hiccup on the devices scan shouldn't also take out the processes scan.
"""
from __future__ import annotations

import threading
import time

from ghost import collectors, store

SCAN_INTERVAL_SECONDS = 300.0     # 5 minutes
PRUNE_INTERVAL_SECONDS = 86400.0  # once a day
EVENT_LOOKBACK_ON_FIRST_SCAN = 3600.0  # first-ever windows_events scan looks back 1h, not forever


class GhostEngine:
    def __init__(self):
        self._thread: threading.Thread | None = None
        self._running = False
        self.scan_count = 0
        self.last_scan_at: float | None = None
        self.last_scan_counts: dict[str, dict[str, int]] = {}
        self.last_error: str | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="GhostEngine")
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        last_prune = time.time()
        while self._running:
            try:
                self._scan_once()
            except Exception as e:
                self.last_error = str(e)
                print(f"[Ghost] Scan cycle failed: {e}")

            if time.time() - last_prune >= PRUNE_INTERVAL_SECONDS:
                try:
                    removed = store.prune_old_events()
                    if removed:
                        print(f"[Ghost] Pruned {removed} events older than the retention window.")
                except Exception as e:
                    print(f"[Ghost] Prune failed: {e}")
                last_prune = time.time()

            time.sleep(SCAN_INTERVAL_SECONDS)

    def _scan_once(self) -> None:
        cycle_counts: dict[str, dict[str, int]] = {}
        for category, collector in collectors.STATE_COLLECTORS.items():
            try:
                current = collector()
                counts = store.record_snapshot(category, current)
                cycle_counts[category] = counts
            except Exception as e:
                print(f"[Ghost] Collector '{category}' failed: {e}")

        try:
            cursor_raw = store.get_meta("events_cursor")
            since_ts = float(cursor_raw) if cursor_raw else (time.time() - EVENT_LOOKBACK_ON_FIRST_SCAN)
            new_events = collectors.windows_events(since_ts)
            n = store.record_events_raw("windows_event", new_events)
            newest_ts = max((e["ts"] for e in new_events), default=since_ts)
            store.set_meta("events_cursor", str(max(newest_ts, since_ts)))
            if n:
                cycle_counts["windows_event"] = {"new": n, "removed": 0, "changed": 0}
        except Exception as e:
            print(f"[Ghost] Windows Events collector failed: {e}")

        self.scan_count += 1
        self.last_scan_at = time.time()
        self.last_scan_counts = cycle_counts

        total_changes = sum(sum(c.values()) for c in cycle_counts.values())
        if self.scan_count == 1:
            print(f"[Ghost] Baseline established ({len(collectors.STATE_COLLECTORS)} categories + windows_event).")
        elif total_changes:
            summary = ", ".join(
                f"{cat}: +{c.get('new', 0)}/-{c.get('removed', 0)}/~{c.get('changed', 0)}"
                for cat, c in cycle_counts.items() if sum(c.values())
            )
            print(f"[Ghost] Scan #{self.scan_count}: {summary}")


# Module-level singleton, mirroring ui/sys_metrics.py's `_metrics` pattern --
# but NOT auto-started on import (unlike _metrics): main.py's run() calls
# .start() explicitly alongside dashboard/telegram, so scan timing is
# controlled from one place instead of firing the moment anything imports
# this module. actions/digital_ghost.py imports this same object to read
# .scan_count/.last_scan_at for its 'status' action.
ghost_engine = GhostEngine()
