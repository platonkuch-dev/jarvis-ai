"""
windows_control — the Windows Computer Control Layer for JARVIS.

Sits below the existing actions/*.py tool layer:

    JARVIS (voice) -> LLM (Gemini Live) -> Action Router (actions/*.py, main.py)
        -> Windows Control Layer (this package) -> Target Application

Layers, tried in order by the router (fast/reliable first, expensive/fuzzy last):

    Level 1  native.py    — Win32 API: processes, windows, focus, move/resize, kill.
    Level 2  uia.py        — UI Automation: find/click/type/read elements, UI tree.
    Level 3  keyboard_mouse.py — pyautogui fallback: raw keyboard/mouse.
    Level 4  vision.py     — screenshot + Claude/Gemini vision element location.

router.py ties them together into single actions (click/type/find_element/...)
with automatic fallback, structured logging and execution-context tracking.
context.py holds the "what am I looking at right now" state so commands like
"click it" can resolve after "find the Settings button".
"""

from .context import get_context, reset_context
from . import native, uia, vision, keyboard_mouse, router, app_resolver
from .logging_utils import log_action

__all__ = [
    "get_context", "reset_context",
    "native", "uia", "vision", "keyboard_mouse", "router", "app_resolver",
    "log_action",
]
