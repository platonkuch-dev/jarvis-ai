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

so a human (or JARVIS itself, if it ever reads its own log) can see exactly
what was tried and what happened, including fallback chains.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

try:
    from core.path_utils import get_user_data_dir
except Exception:  # pragma: no cover - keeps module importable in isolation
    def get_user_data_dir() -> Path:
        p = Path.home() / "Jarvis"
        p.mkdir(parents=True, exist_ok=True)
        return p

_LOCK = threading.Lock()
_LOG_PATH = None


def _log_path() -> Path:
    global _LOG_PATH
    if _LOG_PATH is None:
        _LOG_PATH = get_user_data_dir() / "windows_control.log"
    return _LOG_PATH


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
    print(f"[WinControl]\n{block}")

    try:
        with _LOCK:
            with open(_log_path(), "a", encoding="utf-8") as f:
                f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n{block}\n\n")
    except Exception:
        pass  # logging must never break the actual action
