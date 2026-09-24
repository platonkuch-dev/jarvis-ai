"""
Level 1 — Native Windows control.

Pure Win32 API (via pywin32) + psutil. No UI Automation, no mouse/keyboard
emulation — this is the fast, reliable layer used whenever the job can be
done through the OS directly: enumerating windows/processes, focusing,
minimizing/maximizing/restoring, moving/resizing, closing, killing.

Everything here is a no-op / returns False|[]/None on non-Windows so the
module stays importable (and harmless) if this codebase is ever run cross
platform — but the real target is Windows, per the project's own tooling
(pyautogui win-hotkeys, pycaw, winreg, etc. elsewhere in actions/*.py).
"""
from __future__ import annotations

import platform
import time
from dataclasses import dataclass

_IS_WINDOWS = platform.system() == "Windows"

if _IS_WINDOWS:
    import ctypes
    import win32api
    import win32con
    import win32gui
    import win32process
    import psutil

    _user32 = ctypes.windll.user32
else:  # pragma: no cover
    win32gui = win32process = win32con = win32api = psutil = None  # type: ignore


# Processes we refuse to touch even if asked — killing these can take the
# whole session or the OS itself down.
PROTECTED_PROCESS_NAMES = {
    "system", "system idle process", "registry",
    "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "lsass.exe", "smss.exe", "explorer.exe", "dwm.exe",
    "svchost.exe", "sihost.exe", "fontdrvhost.exe", "taskhostw.exe",
}


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    process_name: str
    is_minimized: bool
    is_maximized: bool
    is_active: bool
    rect: tuple  # (left, top, right, bottom)

    def as_dict(self) -> dict:
        return {
            "hwnd": self.hwnd, "title": self.title, "pid": self.pid,
            "process_name": self.process_name,
            "is_minimized": self.is_minimized, "is_maximized": self.is_maximized,
            "is_active": self.is_active, "rect": self.rect,
        }


@dataclass
class ProcessInfo:
    pid: int
    name: str
    exe: str = ""
    cpu_percent: float = 0.0
    memory_mb: float = 0.0

    def as_dict(self) -> dict:
        return {
            "pid": self.pid, "name": self.name, "exe": self.exe,
            "cpu_percent": self.cpu_percent, "memory_mb": self.memory_mb,
        }


def _require_windows():
    if not _IS_WINDOWS:
        raise RuntimeError("windows_control.native requires Windows.")


def _window_text(hwnd) -> str:
    try:
        length = win32gui.GetWindowTextLength(hwnd)
        if length == 0:
            return ""
        return win32gui.GetWindowText(hwnd)
    except Exception:
        return ""


def _is_maximized(hwnd) -> bool:
    # pywin32's win32gui has no IsZoomed binding — GetWindowPlacement's
    # showCmd is the documented way to check maximized state instead.
    try:
        placement = win32gui.GetWindowPlacement(hwnd)
        return placement[1] == win32con.SW_SHOWMAXIMIZED
    except Exception:
        return False


def _is_real_window(hwnd) -> bool:
    """Filter out tool windows, hidden helper windows, etc. — the same
    heuristic Alt-Tab effectively uses."""
    try:
        if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
            return False
        if win32gui.GetParent(hwnd) != 0:
            return False
        ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        if ex_style & win32con.WS_EX_TOOLWINDOW:
            return False
        if not _window_text(hwnd):
            return False
        return True
    except Exception:
        return False


def list_windows(include_hidden: bool = False) -> list[WindowInfo]:
    """Enumerate top-level windows (Alt-Tab-visible by default)."""
    _require_windows()
    active_hwnd = win32gui.GetForegroundWindow()
    results: list[WindowInfo] = []

    def _cb(hwnd, _):
        try:
            if not include_hidden and not _is_real_window(hwnd):
                return True
            title = _window_text(hwnd)
            if not include_hidden and not title:
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            try:
                pname = psutil.Process(pid).name()
            except Exception:
                pname = ""
            rect = win32gui.GetWindowRect(hwnd)
            results.append(WindowInfo(
                hwnd=hwnd, title=title, pid=pid, process_name=pname,
                is_minimized=bool(win32gui.IsIconic(hwnd)),
                is_maximized=_is_maximized(hwnd),
                is_active=(hwnd == active_hwnd),
                rect=rect,
            ))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_cb, None)
    return results


def get_active_window() -> WindowInfo | None:
    _require_windows()
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return None
    for w in list_windows(include_hidden=True):
        if w.hwnd == hwnd:
            return w
    # Fallback: build directly even if it fails the "real window" filter
    try:
        title = _window_text(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        pname = psutil.Process(pid).name() if pid else ""
        rect = win32gui.GetWindowRect(hwnd)
        return WindowInfo(hwnd, title, pid, pname,
                           bool(win32gui.IsIconic(hwnd)), _is_maximized(hwnd),
                           True, rect)
    except Exception:
        return None


def _stem(s: str) -> str:
    return (s or "").lower().replace(".exe", "").replace(" ", "").strip()


def find_windows(query: str = "", pid: int | None = None) -> list[WindowInfo]:
    """
    Find windows by fuzzy match against title OR process name, or by exact pid.
    `query` matches if it's a substring of the title/process stem or vice versa
    (so "discord" matches "Discord.exe" / "general | my-server - Discord").
    """
    _require_windows()
    windows = list_windows()
    if pid is not None:
        return [w for w in windows if w.pid == pid]

    q = _stem(query)
    if not q:
        return windows

    exact, partial = [], []
    for w in windows:
        title_stem = _stem(w.title)
        proc_stem = _stem(w.process_name)
        if q == proc_stem or q == title_stem:
            exact.append(w)
        elif q in proc_stem or proc_stem in q or q in title_stem:
            partial.append(w)
    return exact + partial


def focus_window(hwnd: int) -> bool:
    """
    Bring a window to the foreground. Windows actively restricts
    SetForegroundWindow from background processes, so this tries three
    escalating strategies before giving up.
    """
    _require_windows()
    if not hwnd or not win32gui.IsWindow(hwnd):
        return False

    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        else:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
    except Exception:
        pass

    # Strategy 1: direct call — works when we already own foreground rights.
    try:
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.05)
        if win32gui.GetForegroundWindow() == hwnd:
            return True
    except Exception:
        pass

    # Strategy 2: AttachThreadInput — the standard trick to borrow input rights.
    try:
        fg_hwnd = win32gui.GetForegroundWindow()
        fg_thread = win32process.GetWindowThreadProcessId(fg_hwnd)[0] if fg_hwnd else 0
        target_thread = win32process.GetWindowThreadProcessId(hwnd)[0]
        cur_thread = win32api.GetCurrentThreadId()

        attached_fg = attached_cur = False
        if fg_thread and fg_thread != target_thread:
            attached_fg = bool(_user32.AttachThreadInput(fg_thread, target_thread, True))
        if cur_thread != target_thread:
            attached_cur = bool(_user32.AttachThreadInput(cur_thread, target_thread, True))
        try:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        finally:
            if attached_fg:
                _user32.AttachThreadInput(fg_thread, target_thread, False)
            if attached_cur:
                _user32.AttachThreadInput(cur_thread, target_thread, False)
        time.sleep(0.05)
        if win32gui.GetForegroundWindow() == hwnd:
            return True
    except Exception:
        pass

    # Strategy 3: the classic ALT-key nudge — a synthetic key event resets the
    # "last input was from the user" flag the foreground lock relies on.
    try:
        _user32.keybd_event(0x12, 0, 0, 0)   # VK_MENU down
        win32gui.SetForegroundWindow(hwnd)
        _user32.keybd_event(0x12, 0, 0x0002, 0)  # VK_MENU up (KEYEVENTF_KEYUP)
        time.sleep(0.05)
        return win32gui.GetForegroundWindow() == hwnd
    except Exception:
        return False


def minimize_window(hwnd: int) -> bool:
    _require_windows()
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        return True
    except Exception:
        return False


def maximize_window(hwnd: int) -> bool:
    _require_windows()
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
        return True
    except Exception:
        return False


def restore_window(hwnd: int) -> bool:
    _require_windows()
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        return True
    except Exception:
        return False


def close_window(hwnd: int) -> bool:
    """Politely ask the window to close (WM_CLOSE) — lets the app prompt to
    save, confirm, etc. rather than yanking the process out from under it."""
    _require_windows()
    try:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        return True
    except Exception:
        return False


def move_resize_window(hwnd: int, x: int | None = None, y: int | None = None,
                        width: int | None = None, height: int | None = None) -> bool:
    _require_windows()
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        cur_w, cur_h = right - left, bottom - top
        new_x = left if x is None else x
        new_y = top if y is None else y
        new_w = cur_w if width is None else width
        new_h = cur_h if height is None else height
        win32gui.MoveWindow(hwnd, new_x, new_y, new_w, new_h, True)
        return True
    except Exception:
        return False


def list_processes(name_filter: str = "") -> list[ProcessInfo]:
    _require_windows()
    results = []
    q = _stem(name_filter)
    for p in psutil.process_iter(["pid", "name", "exe"]):
        try:
            info = p.info
            name = info.get("name") or ""
            if q and q not in _stem(name):
                continue
            mem_mb = 0.0
            try:
                mem_mb = p.memory_info().rss / (1024 * 1024)
            except Exception:
                pass
            results.append(ProcessInfo(
                pid=info.get("pid", 0), name=name, exe=info.get("exe") or "",
                cpu_percent=0.0, memory_mb=round(mem_mb, 1),
            ))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return results


def kill_process(pid: int, force: bool = False) -> tuple[bool, str]:
    _require_windows()
    try:
        proc = psutil.Process(pid)
        name = (proc.name() or "").lower()
        if name in PROTECTED_PROCESS_NAMES:
            return False, f"Refusing to close protected system process '{name}'."
        if force:
            proc.kill()
            return True, f"Force-killed {name} (pid {pid})."
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except psutil.TimeoutExpired:
            proc.kill()
        return True, f"Closed {name} (pid {pid})."
    except psutil.NoSuchProcess:
        return True, "Process was already gone."
    except Exception as e:
        return False, str(e)


def wait_for_window(query: str = "", pid: int | None = None,
                     timeout: float = 10.0, poll: float = 0.3) -> WindowInfo | None:
    """Poll until a matching window appears, instead of a blind sleep()."""
    _require_windows()
    deadline = time.monotonic() + timeout
    while True:
        matches = find_windows(query=query, pid=pid)
        if matches:
            return matches[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)


def wait_for_process(name: str, timeout: float = 10.0, poll: float = 0.3) -> ProcessInfo | None:
    _require_windows()
    deadline = time.monotonic() + timeout
    while True:
        matches = list_processes(name_filter=name)
        if matches:
            return matches[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)


def wait_for_window_gone(hwnd: int, timeout: float = 10.0, poll: float = 0.3) -> bool:
    _require_windows()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not win32gui.IsWindow(hwnd):
            return True
        time.sleep(poll)
    return not win32gui.IsWindow(hwnd)
