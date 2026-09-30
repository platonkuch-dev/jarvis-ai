"""
Global push-to-talk hotkey — press a key combo anywhere on the system to
start Fast Path listening immediately, instead of saying "джарвис".

Uses the Win32 RegisterHotKey API directly via ctypes rather than a
third-party global-hotkey package (`keyboard`, `pynput`, ...): ctypes and
pywin32 are already effectively hard dependencies of this Windows-only
project (see windows_control/*.py), so this adds zero new requirements.

RegisterHotKey needs a thread that runs its own Win32 message loop, so
this spins up one dedicated background thread and never touches Qt's or
asyncio's event loop directly — `on_press` fires on that background
thread, and callers that need the asyncio loop (main.py does) must hop
over themselves via `loop.call_soon_threadsafe(...)`, exactly like
JarvisLive._trigger_wake() already does for other off-loop triggers.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import threading
from typing import Callable

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000  # one WM_HOTKEY per physical press, not one per OS key-repeat tick

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

HOTKEY_ID = 1

# Default combo: F10, no modifiers. Change here to rebind.
DEFAULT_MODIFIERS = MOD_NOREPEAT
DEFAULT_VK = 0x79  # virtual-key code for F10


class GlobalHotkey:
    """Registers one system-wide hotkey and calls `on_press` (no args)
    every time it fires, until stop() is called."""

    def __init__(
        self,
        on_press: Callable[[], None],
        modifiers: int = DEFAULT_MODIFIERS,
        vk: int = DEFAULT_VK,
    ):
        self._on_press = on_press
        self._modifiers = modifiers
        self._vk = vk
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._registered = threading.Event()
        self._ok = False

    def start(self) -> bool:
        """Registers the hotkey and starts listening on a daemon thread.
        Returns whether registration actually succeeded (fails if the
        combo is already claimed by another app, or this isn't Windows)."""
        if os.name != "nt":
            print("[Hotkey] Global hotkey requires Windows — skipped.")
            return False
        if self._thread is not None:
            return self._ok
        self._thread = threading.Thread(target=self._run, name="jarvis-hotkey", daemon=True)
        self._thread.start()
        self._registered.wait(timeout=5.0)
        return self._ok

    @property
    def ok(self) -> bool:
        return self._ok

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, ctypes.c_uint, ctypes.c_uint]
        user32.GetMessageW.restype = ctypes.c_int
        user32.PostThreadMessageW.argtypes = [wintypes.DWORD, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostThreadMessageW.restype = wintypes.BOOL

        self._thread_id = kernel32.GetCurrentThreadId()
        self._ok = bool(user32.RegisterHotKey(None, HOTKEY_ID, self._modifiers, self._vk))
        self._registered.set()
        if not self._ok:
            print("[Hotkey] RegisterHotKey failed — combo may already be in use by another app.")
            return

        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    try:
                        self._on_press()
                    except Exception as e:
                        print(f"[Hotkey] on_press handler failed: {e}")
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)

    def stop(self) -> None:
        if self._thread_id is not None:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
