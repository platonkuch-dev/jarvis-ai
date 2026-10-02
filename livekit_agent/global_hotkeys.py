"""Global hotkeys through Win32 RegisterHotKey, with the `keyboard` package as a fallback.

Why not just `keyboard.add_hotkey`: it is a low-level keyboard hook, and Windows silently
removes such a hook when its callback is slow to return (LowLevelHooksTimeout). Under load
-- the agent's event loop stalling for hundreds of ms, the GIL busy -- that happens, and
F9/F10 then stay dead until a restart. RegisterHotKey has no callback in the input path:
Windows posts WM_HOTKEY to our own thread's message queue, so it can't be dropped like that.

The catch: a registered hotkey is consumed (other apps no longer see that key), and
registration fails if another program already owns the combination -- then this falls back
to the hook so the key still works the old way.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable

logger = logging.getLogger("jarvis-voice-agent.global_hotkeys")

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
_MODS = {"alt": MOD_ALT, "ctrl": MOD_CONTROL, "control": MOD_CONTROL, "shift": MOD_SHIFT,
         "win": MOD_WIN, "windows": MOD_WIN}
_NAMED_VK = {"space": 0x20, "pause": 0x13, "insert": 0x2D, "home": 0x24, "end": 0x23,
             "page up": 0x21, "page down": 0x22, "scroll lock": 0x91}


def parse(combo: str) -> tuple[int, int] | None:
    """'f10' / 'ctrl+alt+j' -> (modifiers, virtual key), or None if not understood."""
    mods, vk = 0, None
    for part in (p.strip().lower() for p in combo.split("+")):
        if part in _MODS:
            mods |= _MODS[part]
        elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            vk = 0x70 + int(part[1:]) - 1
        elif len(part) == 1 and part.isascii() and part.isalnum():
            vk = ord(part.upper())
        elif part in _NAMED_VK:
            vk = _NAMED_VK[part]
        else:
            return None
    return (mods, vk) if vk is not None else None


class GlobalHotkeys:
    """Owns one message-loop thread; add() before start(). Callbacks run on that thread."""

    def __init__(self) -> None:
        self._wanted: list[tuple[str, Callable[[], None]]] = []
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._hooked: list[str] = []          # combos that fell back to the keyboard hook

    def add(self, combo: str, callback: Callable[[], None]) -> None:
        self._wanted.append((combo, callback))

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="global_hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(3.0)

    def stop(self) -> None:
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._hooked:
            import keyboard

            for combo in self._hooked:
                try:
                    keyboard.remove_hotkey(combo)
                except (KeyError, ValueError):
                    pass
            self._hooked.clear()

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        callbacks: dict[int, Callable[[], None]] = {}
        for i, (combo, cb) in enumerate(self._wanted, start=1):
            parsed = parse(combo)
            if parsed and user32.RegisterHotKey(None, i, parsed[0] | MOD_NOREPEAT, parsed[1]):
                callbacks[i] = cb
                logger.info("hotkey %s registered (RegisterHotKey)", combo)
            else:
                self._fallback(combo, cb)
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam in callbacks:
                    try:
                        callbacks[msg.wParam]()
                    except Exception:
                        logger.exception("hotkey callback failed")
        finally:
            for i in callbacks:
                user32.UnregisterHotKey(None, i)

    def _fallback(self, combo: str, cb: Callable[[], None]) -> None:
        try:
            import keyboard

            keyboard.add_hotkey(combo, cb)
            self._hooked.append(combo)
            logger.warning("hotkey %s is taken by another program or unknown -- using the keyboard hook", combo)
        except Exception:
            logger.exception("could not register hotkey %s at all", combo)
