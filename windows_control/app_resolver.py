"""
Smart application resolution / launch verification.

actions/open_app.py already implements the hard part — a layered launcher
(PATH -> Windows "App Paths" registry -> Start Menu shortcuts -> Start Menu
search) that finds an app without ever hardcoding "C:\\Program Files\\...".
This module does NOT duplicate that. It adds the two things the launcher
itself has no way to know:

  1. "Is this app already running?" — checked FIRST, before ever spawning a
     new process, by matching windows/processes against the same alias table
     open_app.py uses (so "Discord" and "discord.exe" resolve the same way).
  2. "Did a real window actually appear after launch?" — actions/open_app.py
     verifies a *process* started; this verifies a *window* showed up and
     focuses it, then records it in the shared ExecutionContext so the next
     window_manager/ui_automation call can default to "the app we just
     opened" without the caller repeating its name.
"""
from __future__ import annotations

from dataclasses import dataclass

from actions.open_app import _normalize, _SYSTEM, open_app as _launch

from . import native
from .context import get_context


@dataclass
class LaunchResult:
    success: bool
    message: str
    already_running: bool = False
    window: native.WindowInfo | None = None


def _candidate_stems(app_name: str) -> set[str]:
    stems = {app_name.lower().replace(" ", "")}
    normalized = _normalize(app_name)
    stems.add(normalized.lower().replace(".exe", "").replace(" ", ""))
    return {s for s in stems if s}


def find_running(app_name: str) -> native.WindowInfo | None:
    """Best-effort match of an already-running window for this app name."""
    if _SYSTEM != "Windows":
        return None
    candidates = _candidate_stems(app_name)
    windows = native.list_windows()
    for w in windows:
        proc_stem = w.process_name.lower().replace(".exe", "").replace(" ", "")
        title_stem = w.title.lower().replace(" ", "")
        for cand in candidates:
            if cand and (cand == proc_stem or cand in proc_stem or proc_stem in cand or cand in title_stem):
                return w
    return None


def launch_or_focus(app_name: str, wait_timeout: float = 8.0) -> LaunchResult:
    """
    1. Already running?  -> focus it, done.
    2. Otherwise launch via the existing multi-strategy open_app(), then wait
       for a real window to appear (native.wait_for_window) and focus it.
    Always updates the shared ExecutionContext on success.
    """
    ctx = get_context()

    running = find_running(app_name)
    if running:
        native.focus_window(running.hwnd)
        ctx.set_active(app=app_name, window_title=running.title,
                        hwnd=running.hwnd, pid=running.pid)
        return LaunchResult(True, f"{app_name} was already running — focused it.",
                             already_running=True, window=running)

    launch_message = _launch(parameters={"app_name": app_name})
    window = native.wait_for_window(query=app_name, timeout=wait_timeout)

    if window:
        native.focus_window(window.hwnd)
        ctx.set_active(app=app_name, window_title=window.title,
                        hwnd=window.hwnd, pid=window.pid)
        return LaunchResult(True, f"{launch_message} Window confirmed and focused.",
                             window=window)

    # Process may have started without a (yet) discoverable top-level window
    # (tray apps, slow-loading Electron splash screens, etc.) — don't treat
    # that as failure, just say so honestly instead of claiming a window exists.
    ctx.set_active(app=app_name)
    return LaunchResult(True, f"{launch_message} (No top-level window detected yet.)")
