"""
Startup self-check for the Windows Control Layer.

Cheap, fast (no screenshots, no process spawning) sanity checks that each
layer's underlying dependency actually works on this machine — run once
when JARVIS connects, logged once, never blocking startup. This is NOT a
health monitor; it's a "did the environment come up sane" check, matching
the rest of this codebase's existing pattern of trying something and
falling back / logging clearly rather than crashing (see main.py's own
dashboard/Telegram optional-subsystem try/excepts).
"""
from __future__ import annotations

import platform


def run_self_check() -> dict[str, tuple[bool, str]]:
    """Returns {component: (ok, detail)}. Never raises."""
    results: dict[str, tuple[bool, str]] = {}

    if platform.system() != "Windows":
        results["platform"] = (False, f"{platform.system()} — Windows Control Layer is Windows-only")
        return results
    results["platform"] = (True, "Windows")

    # Level 1 — native Win32 API (window/process enumeration)
    try:
        from . import native
        windows = native.list_windows()
        results["native_win32"] = (True, f"{len(windows)} windows enumerated")
    except Exception as e:
        results["native_win32"] = (False, str(e))

    # Level 2 — UI Automation (pywinauto/comtypes/COM)
    try:
        from . import uia
        uia.get_desktop()
        results["ui_automation"] = (True, "pywinauto UIA backend available")
    except Exception as e:
        results["ui_automation"] = (False, str(e))

    # Level 3 — keyboard/mouse (pyautogui)
    try:
        import pyautogui
        _ = pyautogui.size()
        results["keyboard_mouse"] = (True, "pyautogui available")
    except Exception as e:
        results["keyboard_mouse"] = (False, str(e))

    # Level 4 — screenshot subsystem (pyautogui + Pillow, no actual capture here)
    try:
        import pyautogui  # noqa: F401 (re-import is cheap/cached, keeps this check independent)
        from PIL import Image  # noqa: F401
        results["screenshot_subsystem"] = (True, "pyautogui + Pillow available")
    except Exception as e:
        results["screenshot_subsystem"] = (False, str(e))

    # Process management (psutil)
    try:
        import psutil
        _ = psutil.Process()
        results["process_management"] = (True, "psutil available")
    except Exception as e:
        results["process_management"] = (False, str(e))

    return results


def summarize(results: dict[str, tuple[bool, str]]) -> str:
    total = len(results)
    ok = sum(1 for ok, _ in results.values() if ok)
    if ok == total:
        return f"Windows Control Layer ready ({ok}/{total} checks passed)."
    failed = [f"{k} ({detail})" for k, (passed, detail) in results.items() if not passed]
    return f"Windows Control Layer: {ok}/{total} checks passed. Issues: {'; '.join(failed)}"
