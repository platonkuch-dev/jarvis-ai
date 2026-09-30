"""
Voice-tool front end for Digital Ghost (ghost/engine.py + ghost/store.py):
"what changed" queries against the background baseline, matching the
user's own 'Time Machine' example format ('покажи, что изменилось за
последние 6 часов'). Read-only -- this module never touches ghost_state
or ghost_events itself, only queries them.
"""
from __future__ import annotations

import time
from datetime import datetime

from ghost import store
from ghost.engine import ghost_engine

_CATEGORY_ORDER = [
    "processes", "files", "registry", "services",
    "network", "dns", "devices", "applications", "windows_event",
]
_CATEGORY_LABEL = {
    "processes": "processes",
    "files": "files",
    "registry": "startup/persistence entries",
    "services": "services",
    "network": "network destinations",
    "dns": "DNS lookups",
    "devices": "devices",
    "applications": "applications",
    "windows_event": "system events",
}


def _format_delta(delta: dict) -> str:
    hours = delta["since_hours"]
    lines = [f"SYSTEM DELTA (last {hours:g}h)", ""]

    any_change = False
    for category in _CATEGORY_ORDER:
        counts = delta["by_category"].get(category)
        if not counts:
            continue
        label = _CATEGORY_LABEL.get(category, category)
        new_n, removed_n, changed_n = counts.get("new", 0), counts.get("removed", 0), counts.get("changed", 0)
        if new_n:
            lines.append(f"+ {new_n} {label}")
            any_change = True
        if removed_n:
            lines.append(f"- {removed_n} {label}")
            any_change = True
        if changed_n:
            lines.append(f"~ {changed_n} {label} changed")
            any_change = True

    if not any_change:
        return f"SYSTEM DELTA (last {hours:g}h)\n\nNo changes detected — baseline stable."

    first = delta["first_event"]
    if first:
        first_time = datetime.fromtimestamp(first["ts"]).strftime("%H:%M:%S")
        lines.append("")
        lines.append(f"FIRST CHANGE: {first_time}  ({first['category']} {first['change']}: {first['item_key'][:80]})")

    return "\n".join(lines)


def _format_status() -> str:
    st = store.status()
    lines = ["GHOST STATUS", ""]
    if ghost_engine.scan_count == 0:
        lines.append("Baseline not established yet — first scan still running.")
    else:
        last_scan_ago = time.time() - (ghost_engine.last_scan_at or time.time())
        lines.append(f"Scans completed: {ghost_engine.scan_count}  (last: {int(last_scan_ago)}s ago)")
        lines.append(f"Total tracked events: {st['total_events']}")
        lines.append("")
        for category in _CATEGORY_ORDER:
            if category in st["categories"]:
                label = _CATEGORY_LABEL.get(category, category)
                lines.append(f"  {label}: {st['categories'][category]} tracked")
    return "\n".join(lines)


def digital_ghost(parameters: dict | None = None, player=None) -> str:
    parameters = parameters or {}
    action = str(parameters.get("action", "delta")).strip().lower()

    if ghost_engine.scan_count == 0:
        msg = "Digital Ghost is still building its first baseline snapshot — try again in a moment."
        if player is not None and hasattr(player, "show_content"):
            player.show_content("DIGITAL GHOST", msg)
        return msg

    if action == "status":
        body = _format_status()
        if player is not None and hasattr(player, "show_content"):
            player.show_content("DIGITAL GHOST", body)
        return (
            f"Ghost has completed {ghost_engine.scan_count} scans and is tracking "
            f"{sum(store.status()['categories'].values())} items across "
            f"{len(store.status()['categories'])} categories."
        )

    try:
        hours = float(parameters.get("hours", 6))
    except (TypeError, ValueError):
        hours = 6.0
    hours = max(0.1, min(hours, 24.0 * 30))

    delta = store.query_delta(hours)
    body = _format_delta(delta)
    if player is not None and hasattr(player, "show_content"):
        player.show_content("DIGITAL GHOST — TIME MACHINE", body)

    if delta["total_events"] == 0:
        return f"No changes in the last {hours:g} hours — system matches baseline."

    parts = []
    for category in _CATEGORY_ORDER:
        counts = delta["by_category"].get(category)
        if not counts:
            continue
        label = _CATEGORY_LABEL.get(category, category)
        bits = []
        if counts.get("new"):
            bits.append(f"{counts['new']} new")
        if counts.get("removed"):
            bits.append(f"{counts['removed']} removed")
        if counts.get("changed"):
            bits.append(f"{counts['changed']} changed")
        if bits:
            parts.append(f"{label}: {', '.join(bits)}")

    summary = "; ".join(parts[:4])
    return f"In the last {hours:g} hours: {summary}. Full breakdown in the panel."
