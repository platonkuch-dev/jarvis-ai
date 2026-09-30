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


# US-layout virtual keys for printable ASCII (0x100 = needs Shift), as
# VkKeyScan would report them under an English layout.
_US_SHIFTED = {"!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0"}
_US_OEM = {";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0,
           "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE}
_US_OEM_SHIFTED = {":": ";", "+": "=", "<": ",", "_": "-", ">": ".", "?": "/", "~": "`",
                   "{": "[", "|": "\\", "}": "]", '"': "'"}


def _us_vk(c: str) -> int | None:
    if "a" <= c <= "z" or "0" <= c <= "9":
        return ord(c.upper())
    if "A" <= c <= "Z":
        return 0x100 | ord(c)
    if c == " ":
        return 0x20
    if c in _US_SHIFTED:
        return 0x100 | ord(_US_SHIFTED[c])
    if c in _US_OEM:
        return _US_OEM[c]
    if c in _US_OEM_SHIFTED:
        return 0x100 | _US_OEM[_US_OEM_SHIFTED[c]]
    return None


def fix_key_mapping() -> int:
    """pyautogui builds its key table once, at import, with VkKeyScan under
    the keyboard layout active at that moment. Under a Cyrillic layout
    (Russian/Ukrainian) every Latin letter comes back as -1, and pyautogui
    then turns divmod(-1, 256) into "Alt+Ctrl+Shift + VK 255": Ctrl+A,
    Ctrl+L, Ctrl+V, Ctrl+S ... all silently misfire. Shortcuts are
    layout-independent virtual keys anyway, so the missing entries get their
    US-layout VK codes. Returns how many entries were fixed."""
    if not _PYAUTOGUI:
        return 0
    try:
        from pyautogui import _pyautogui_win as win
    except Exception:  # pragma: no cover - not Windows
        return 0
    fixed = 0
    for code in range(32, 128):
        c = chr(code)
        current = win.keyboardMapping.get(c)
        if current is None or current == -1 or current < 0:
            vk = _us_vk(c)
            if vk is not None:
                win.keyboardMapping[c] = vk
                fixed += 1
    return fixed


fix_key_mapping()


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


# ── clipboard (Win32, via ctypes) ───────────────────────────────────────
#
# Own implementation instead of pyperclip so the text we put there is marked
# private: Windows 11 clipboard history (Win+V) and cloud clipboard sync skip
# it. That matters because the computer-use agent pastes generated passwords.

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002


def _clipboard_api():
    import ctypes
    from ctypes import wintypes

    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterClipboardFormatW.restype = wintypes.UINT
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    return ctypes, user32, kernel32


def _open_clipboard(user32) -> None:
    # Another process (clipboard managers, the app we just pasted into) can
    # hold it for a moment.
    for _ in range(20):
        if user32.OpenClipboard(None):
            return
        time.sleep(0.02)
    raise OSError("буфер обмена занят другим приложением")


def _global_copy(ctypes, kernel32, data: bytes):
    handle = kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
    if not handle:
        raise OSError("GlobalAlloc failed")
    ptr = kernel32.GlobalLock(handle)
    ctypes.memmove(ptr, data, len(data))
    kernel32.GlobalUnlock(handle)
    return handle


def get_clipboard_text() -> str | None:
    """Current clipboard text, or None if it holds no text (image, files, empty)."""
    ctypes, user32, kernel32 = _clipboard_api()
    if not user32.IsClipboardFormatAvailable(_CF_UNICODETEXT):
        return None
    _open_clipboard(user32)
    try:
        handle = user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        try:
            return ctypes.wstring_at(ptr) if ptr else None
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str, private: bool = True) -> None:
    ctypes, user32, kernel32 = _clipboard_api()
    data = (text + "\0").encode("utf-16-le")
    _open_clipboard(user32)
    try:
        user32.EmptyClipboard()
        handle = _global_copy(ctypes, kernel32, data)
        if not user32.SetClipboardData(_CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            raise OSError("SetClipboardData failed")
        if private:
            zero = (0).to_bytes(4, "little")
            for name, payload in (("ExcludeClipboardContentFromMonitorProcessing", b"\0"),
                                  ("CanIncludeInClipboardHistory", zero),
                                  ("CanUploadToCloudClipboard", zero)):
                fmt = user32.RegisterClipboardFormatW(name)
                if fmt:
                    user32.SetClipboardData(fmt, _global_copy(ctypes, kernel32, payload))
    finally:
        user32.CloseClipboard()


def paste_text(text: str, restore: bool = True) -> str:
    """Faster + more reliable for long/unicode/multi-line text than keystroke
    typing: one Ctrl+V, nothing for the target app to drop. The user's own
    clipboard text is put back afterwards."""
    _require()
    previous = None
    if restore:
        try:
            previous = get_clipboard_text()
        except OSError:
            previous = None
    try:
        set_clipboard_text(text)
    except OSError:
        if not _PYPERCLIP:
            raise
        pyperclip.copy(text)
    time.sleep(0.05)
    pyautogui.hotkey("ctrl", "v")
    if previous is not None:
        # The target reads the clipboard asynchronously after Ctrl+V; swapping
        # it back too early would paste the old content instead.
        time.sleep(0.25)
        try:
            set_clipboard_text(previous, private=False)
        except OSError:
            pass
    return f"Pasted: {text[:60]}"


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
