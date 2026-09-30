"""Multiple monitors: list them, capture one, and drive the mouse on it.

pyautogui only ever sees the primary monitor (its size() and screenshot()
are primary-only), so everything that "looks" at the screen went blind on a
second display. Here:

  monitors()          -> [Monitor(1=primary), Monitor(2), ...] in virtual-screen pixels
  grab(n)             -> PIL image of monitor n (0 = all monitors stitched)
  pyautogui_for(n)    -> a drop-in stand-in for the pyautogui module whose
                         size()/screenshot()/click()/moveTo()/... work in that
                         monitor's own coordinates (0,0 = its top-left corner)
  set_active(n) / active_pyautogui()
                      -> what tools/computer_use.py uses for the current run

Numbering is what people say out loud: 1 = main monitor, 2 = the other one;
extra monitors follow left-to-right.

The process is made per-monitor DPI aware before pyautogui is imported
anywhere, so coordinates are real pixels on every monitor even when they use
different Windows scaling (125 % on a laptop, 150 % on a 4K screen, ...).
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass
from typing import Any

_DPI_SET = False


def _ensure_dpi_awareness() -> None:
    global _DPI_SET
    if _DPI_SET or sys.platform != "win32":
        return
    _DPI_SET = True
    try:  # Windows 10 1703+: per-monitor v2
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


_ensure_dpi_awareness()


class _PerMonitorDpi:
    """Per-monitor DPI for this thread only, for the duration of a block.
    The process-wide call above fails if something (pyautogui imports call
    SetProcessDPIAware) got there first; this makes monitor rects, captures
    and cursor positions agree in real pixels either way."""

    def __enter__(self):
        self._old = None
        if sys.platform == "win32":
            try:
                fn = ctypes.windll.user32.SetThreadDpiAwarenessContext
                fn.restype = ctypes.c_void_p
                self._old = fn(ctypes.c_void_p(-4))
            except Exception:
                self._old = None
        return self

    def __exit__(self, *exc):
        if self._old:
            ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(self._old))
        return False


_dpi = _PerMonitorDpi


@dataclass(frozen=True)
class Monitor:
    index: int
    left: int
    top: int
    width: int
    height: int
    primary: bool

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def describe(self) -> str:
        where = "основной" if self.primary else ("слева" if self.left < 0 else "справа" if self.left > 0 else
                                                 "сверху" if self.top < 0 else "снизу")
        return f"монитор {self.index} ({where}, {self.width}×{self.height})"


def monitors() -> list[Monitor]:
    if sys.platform != "win32":
        import pyautogui

        w, h = pyautogui.size()
        return [Monitor(1, 0, 0, w, h, True)]
    from ctypes import wintypes

    rects: list[tuple[int, int, int, int]] = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.POINTER(wintypes.RECT), ctypes.c_double)

    def _cb(_h, _dc, rect, _data):
        r = rect.contents
        rects.append((r.left, r.top, r.right, r.bottom))
        return 1

    with _dpi():
        ctypes.windll.user32.EnumDisplayMonitors(0, 0, proc(_cb), 0)
    primary = [r for r in rects if r[0] == 0 and r[1] == 0]
    others = sorted((r for r in rects if r not in primary), key=lambda r: (r[0], r[1]))
    return [Monitor(i, r[0], r[1], r[2] - r[0], r[3] - r[1], i == 1)
            for i, r in enumerate(primary + others, 1)]


def get(index: int) -> Monitor:
    mons = monitors()
    if index < 1 or index > len(mons):
        raise ValueError(f"Монитора {index} нет — подключено {len(mons)}.")
    return mons[index - 1]


def secondary() -> Monitor | None:
    mons = monitors()
    return mons[1] if len(mons) > 1 else None


def grab(index: int = 1, region: tuple[int, int, int, int] | None = None):
    """Screenshot of monitor `index` (0 = every monitor as one image).
    `region` = (left, top, width, height) inside that monitor."""
    from PIL import ImageGrab

    if index == 0:
        with _dpi():
            return ImageGrab.grab(all_screens=True)
    m = get(index)
    if region is not None:
        left, top, width, height = region
        box = (m.left + left, m.top + top, m.left + left + width, m.top + top + height)
    else:
        box = (m.left, m.top, m.right, m.bottom)
    with _dpi():
        return ImageGrab.grab(bbox=box, all_screens=True)


class MonitorPyAutoGUI:
    """pyautogui, but in one monitor's coordinate space."""

    def __init__(self, monitor: Monitor) -> None:
        import pyautogui

        self._pg = pyautogui
        self.monitor = monitor
        self._dx, self._dy = monitor.left, monitor.top

    def __getattr__(self, name: str) -> Any:  # PAUSE, FailSafeException, hotkey, write, ...
        return getattr(self._pg, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in ("_pg", "monitor", "_dx", "_dy"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._pg, name, value)

    def _abs(self, x, y):
        if x is None or y is None:
            return x, y
        return x + self._dx, y + self._dy

    def size(self):
        return self._pg.Size(self.monitor.width, self.monitor.height)

    def position(self):
        with _dpi():
            x, y = self._pg.position()
        return self._pg.Point(x - self._dx, y - self._dy)

    def screenshot(self, region=None, **_kw):
        return grab(self.monitor.index, region)

    def moveTo(self, x=None, y=None, *a, **kw):
        with _dpi():
            return self._pg.moveTo(*self._abs(x, y), *a, **kw)

    def click(self, x=None, y=None, *a, **kw):
        with _dpi():
            return self._pg.click(*self._abs(x, y), *a, **kw)

    def doubleClick(self, x=None, y=None, *a, **kw):
        with _dpi():
            return self._pg.doubleClick(*self._abs(x, y), *a, **kw)

    def rightClick(self, x=None, y=None, *a, **kw):
        with _dpi():
            return self._pg.rightClick(*self._abs(x, y), *a, **kw)

    def dragTo(self, x=None, y=None, *a, **kw):
        with _dpi():
            return self._pg.dragTo(*self._abs(x, y), *a, **kw)

    def mouseDown(self, x=None, y=None, *a, **kw):
        with _dpi():
            return self._pg.mouseDown(*self._abs(x, y), *a, **kw)

    def mouseUp(self, x=None, y=None, *a, **kw):
        with _dpi():
            return self._pg.mouseUp(*self._abs(x, y), *a, **kw)

    def scroll(self, clicks, x=None, y=None, *a, **kw):
        with _dpi():
            return self._pg.scroll(clicks, *self._abs(x, y), *a, **kw)

    def hscroll(self, clicks, x=None, y=None, *a, **kw):
        with _dpi():
            return self._pg.hscroll(clicks, *self._abs(x, y), *a, **kw)


def pyautogui_for(index: int = 1):
    """The plain pyautogui module for the primary monitor (unchanged
    behaviour), a monitor-shifted stand-in for any other."""
    import pyautogui

    if index in (0, 1):
        return pyautogui
    return MonitorPyAutoGUI(get(index))


_active = 1


def set_active(index: int) -> None:
    global _active
    _active = index


def active_pyautogui():
    return pyautogui_for(_active)


def summary() -> str:
    mons = monitors()
    if len(mons) == 1:
        return "Подключён один монитор."
    return "Мониторы: " + "; ".join(m.describe() for m in mons) + "."
