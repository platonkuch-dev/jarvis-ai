"""
Level 3 — Keyboard / Mouse fallback.

Used by the router ONLY when UI Automation couldn't find or act on a target
(or the caller gave raw coordinates directly). Thin wrapper around pyautogui,
which is already a project dependency (see actions/computer_control.py).
"""
from __future__ import annotations

import time

try:
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05
    _PYAUTOGUI = True
except ImportError:  # pragma: no cover
    _PYAUTOGUI = False

try:
    import pyperclip
    _PYPERCLIP = True
except ImportError:  # pragma: no cover
    _PYPERCLIP = False


def _require():
    if not _PYAUTOGUI:
        raise RuntimeError("pyautogui not installed.")


def click(x: int, y: int, button: str = "left", double: bool = False) -> str:
    _require()
    clicks = 2 if double else 1
    pyautogui.click(x, y, button=button, clicks=clicks)
    return f"{'Double-c' if double else 'C'}licked ({x}, {y}) [{button}]"


def move_to(x: int, y: int, duration: float = 0.2) -> str:
    _require()
    pyautogui.moveTo(x, y, duration=duration)
    return f"Mouse -> ({x}, {y})"


def type_text(text: str, interval: float = 0.03) -> str:
    _require()
    pyautogui.typewrite(text, interval=interval)
    return f"Typed: {text[:60]}"


def paste_text(text: str) -> str:
    """Faster + more reliable for long/unicode text than keystroke typing."""
    _require()
    if _PYPERCLIP:
        pyperclip.copy(text)
        time.sleep(0.1)
        pyautogui.hotkey("ctrl", "v")
        return f"Pasted: {text[:60]}"
    return type_text(text)


def press_key(key: str) -> str:
    _require()
    pyautogui.press(key)
    return f"Pressed: {key}"


def hotkey(*keys: str) -> str:
    _require()
    pyautogui.hotkey(*keys)
    return f"Hotkey: {'+'.join(keys)}"


def scroll(direction: str = "down", amount: int = 3, x: int | None = None, y: int | None = None) -> str:
    _require()
    clicks = amount if direction in ("up", "right") else -amount
    if direction in ("up", "down"):
        pyautogui.scroll(clicks, x=x, y=y)
    else:
        pyautogui.hscroll(clicks, x=x, y=y)
    return f"Scrolled {direction} x{amount}"


def clear_field() -> str:
    _require()
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.08)
    pyautogui.press("delete")
    return "Field cleared"


def screenshot():
    _require()
    return pyautogui.screenshot()
