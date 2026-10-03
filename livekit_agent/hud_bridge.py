"""Tiny JSON-file bridge between the worker process (writer) and hud.py (reader).

worker.py and hud.py run as separate OS processes (app.py spawns worker.py as
a subprocess), so there's no shared Python state between them. A small
atomically-written JSON file, polled every ~200ms by the HUD, is simpler and
more robust here than a socket for a status indicator that doesn't need
sub-frame precision.
"""

from __future__ import annotations

import json
import time
from typing import Any, Literal

import config
from atomic_io import atomic_write_text

STATE_PATH = config.DATA_DIR / "hud_state.json"

Status = Literal["idle", "listening", "thinking", "speaking", "sleeping"]

MAX_LINES = 6


def write_state(status: Status, lines: list[dict[str, str]]) -> None:
    data = {"status": status, "lines": lines[-MAX_LINES:], "updated_at": time.time()}
    atomic_write_text(STATE_PATH, json.dumps(data, ensure_ascii=False))


def read_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "idle", "lines": [], "updated_at": 0.0}
