"""
Structured logging for the Windows Control Layer.

Every action executed through router.py prints (and appends to a rotating
log file) a fixed-field block:

    [APP]    Discord
    [WINDOW] Discord
    [ACTION] click
    [TARGET] Settings
    [METHOD] UI Automation
    [RESULT] SUCCESS
    [TIME]   0.084s

so a human (or Джарвис itself, if it ever reads its own log) can see exactly
what was tried and what happened, including fallback chains.
"""
from __future__ import annotations

import threading
import time

import config

_LOCK = threading.Lock()
_LOG_PATH = config.LOGS_DIR / "windows_control.log"


def log_action(
    action: str,
    target: str = "",
    method: str = "",
    result: str = "",
    app: str = "",
    window: str = "",
    elapsed: float | None = None,
    extra: str = "",
) -> None:
    """Print + persist one structured action-log block."""
    lines = [
        f"[APP]    {app or '-'}",
        f"[WINDOW] {window or '-'}",
        f"[ACTION] {action}",
        f"[TARGET] {target or '-'}",
        f"[METHOD] {method or '-'}",
        f"[RESULT] {result}",
        f"[TIME]   {elapsed:.3f}s" if elapsed is not None else "[TIME]   -",
    ]
    if extra:
        lines.append(f"[NOTE]   {extra}")
    block = "\n".join(lines)
    try:
        # A window/element title can contain characters the current console
        # codepage can't encode (seen: U+200E in a browser tab title on a
        # cp1251 Windows console) -- printing must never crash a tool call,
        # same reasoning as the file-write guard below.
        print(f"[WinControl]\n{block}")
    except Exception:
        pass

    try:
        with _LOCK:
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n{block}\n\n")
    except Exception:
        pass  # logging must never break the actual action
