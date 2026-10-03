"""One-way JSON bridge, worker.py (tools/screen_watch.py) -> hud_bar.py: is
the background screen watcher currently on.

Kept as its own tiny file rather than a field on hud_bridge.py's state:
that file is overwritten wholesale on every status change (idle/listening/
thinking/...), so a second, independent write site in the same process
would race to clobber whichever field it doesn't know about.
"""

from __future__ import annotations

import json
import time
from typing import Any

import config
from atomic_io import atomic_write_text

STATE_PATH = config.DATA_DIR / "screen_watch_state.json"


def write_state(watching: bool) -> None:
    data = {"watching": watching, "updated_at": time.time()}
    atomic_write_text(STATE_PATH, json.dumps(data))


def read_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"watching": False, "updated_at": 0.0}
