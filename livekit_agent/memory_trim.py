"""Hands Jarvis's idle memory back to Windows while he sleeps.

Every Jarvis process (worker, phone worker, HUD, Telegram bridge, ...) keeps
pages it touched once -- model loading, imports, startup -- in its working
set. Asleep none of that is in use, so EmptyWorkingSet moves it out of RAM;
Windows pages back in whatever is touched again (a few ms on wake). Nothing
is freed from the program's point of view and nothing is lost.

Processes are found by their command line pointing into this app folder,
the same way the installer finds them (common.kill_processes_in).
"""

from __future__ import annotations

import gc
import logging
import sys

import config

logger = logging.getLogger("jarvis-voice-agent.memory_trim")

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_SET_QUOTA = 0x0100


def _jarvis_pids() -> list[int]:
    import psutil

    base = str(config.BASE_DIR).lower()
    pids = []
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (p.info["name"] or "").lower()
            if name.startswith("python") and base in " ".join(p.info["cmdline"] or []).lower():
                pids.append(p.pid)
        except Exception:
            continue
    return pids


def trim_all() -> float:
    """Trims every Jarvis process; returns the MB taken out of RAM. Windows only, never raises."""
    if sys.platform != "win32":
        return 0.0
    try:
        import ctypes

        import psutil

        gc.collect()
        kernel32 = ctypes.windll.kernel32
        before = after = 0
        for pid in _jarvis_pids():
            try:
                proc = psutil.Process(pid)
                before += proc.memory_info().rss
                handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_SET_QUOTA, False, pid)
                if handle:
                    kernel32.K32EmptyWorkingSet(handle)
                    kernel32.CloseHandle(handle)
                after += proc.memory_info().rss
            except Exception:
                continue
        freed = (before - after) / 2**20
        logger.info("memory trim: %.0f MB -> %.0f MB in RAM", before / 2**20, after / 2**20)
        return freed
    except Exception:
        logger.exception("memory trim failed")
        return 0.0
