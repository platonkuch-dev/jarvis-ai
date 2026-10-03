"""Atomic small-file writes that survive a concurrent reader on Windows.

The HUD, panel, tray bar and worker are separate processes that share state
through tiny JSON files (hud_state, mic_state, camera/screen-watch state...).
Writing a .tmp and renaming it over the target is atomic, but on Windows the
rename fails with PermissionError (WinError 5/32) for the few milliseconds
another process has the target open for reading -- the write was silently
lost (a stale HUD) or raised into the caller (an F9 press that did nothing).
A short retry is all it takes: readers hold these files for microseconds.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

_RETRIES = 10
_DELAY_S = 0.01


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding=encoding)
    for attempt in range(_RETRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == _RETRIES - 1:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            time.sleep(_DELAY_S * (attempt + 1))
