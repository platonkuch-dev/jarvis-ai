"""
Execution Context — what Джарвис is "currently looking at" on the desktop.

Tracked so that:
  * a follow-up command like "click it" / "нажми её" can resolve against the
    last element that was found or acted on;
  * window_manager / ui_automation calls can default to "the app we just
    launched or focused" when the user doesn't repeat its name;
  * a chain of steps (open app -> wait -> click -> type) shares state without
    the LLM having to re-discover the window/pid on every single step.

Single process-wide instance, guarded by a lock since tool calls run on a
thread-pool executor and could in principle overlap.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class ElementRef:
    """A remembered UI element — enough to re-locate or act on it later."""
    name: str = ""
    control_type: str = ""
    automation_id: str = ""
    class_name: str = ""
    rect: tuple | None = None          # (left, top, right, bottom)
    app: str = ""
    window_title: str = ""
    found_via: str = ""                # "uia" | "vision"
    timestamp: float = field(default_factory=time.monotonic)

    def center(self) -> tuple[int, int] | None:
        if not self.rect:
            return None
        l, t, r, b = self.rect
        return (l + r) // 2, (t + b) // 2

    def is_stale(self, max_age: float = 90.0) -> bool:
        return (time.monotonic() - self.timestamp) > max_age


class ExecutionContext:
    def __init__(self):
        self._lock = threading.Lock()
        self.active_app: str = ""
        self.active_window_title: str = ""
        self.active_hwnd: int | None = None
        self.active_pid: int | None = None
        self.last_elements: list[ElementRef] = []
        self.last_screenshot_path: str | None = None
        self.current_action: str | None = None
        self.current_step: int = 0
        self.history: list[dict] = []
        self._max_history = 60

    # ── active app/window ────────────────────────────────────────────────
    def set_active(self, app: str = "", window_title: str = "",
                    hwnd: int | None = None, pid: int | None = None) -> None:
        with self._lock:
            if app:
                self.active_app = app
            if window_title:
                self.active_window_title = window_title
            if hwnd is not None:
                self.active_hwnd = hwnd
            if pid is not None:
                self.active_pid = pid

    def get_active(self) -> dict:
        with self._lock:
            return {
                "app": self.active_app,
                "window_title": self.active_window_title,
                "hwnd": self.active_hwnd,
                "pid": self.active_pid,
            }

    # ── elements (for "click it" style follow-ups) ──────────────────────
    def remember_elements(self, elements: list[ElementRef]) -> None:
        with self._lock:
            self.last_elements = elements[:25]

    def last_element(self) -> ElementRef | None:
        with self._lock:
            if not self.last_elements:
                return None
            e = self.last_elements[0]
            return None if e.is_stale() else e

    # ── step / action bookkeeping ────────────────────────────────────────
    def begin_action(self, action: str) -> None:
        with self._lock:
            self.current_action = action
            self.current_step += 1

    def record_history(self, entry: dict) -> None:
        with self._lock:
            entry["step"] = self.current_step
            entry["ts"] = time.time()
            self.history.append(entry)
            if len(self.history) > self._max_history:
                self.history = self.history[-self._max_history:]

    def recent_history(self, n: int = 10) -> list[dict]:
        with self._lock:
            return list(self.history[-n:])

    def reset(self) -> None:
        with self._lock:
            self.active_app = ""
            self.active_window_title = ""
            self.active_hwnd = None
            self.active_pid = None
            self.last_elements = []
            self.last_screenshot_path = None
            self.current_action = None
            self.current_step = 0
            self.history = []


_INSTANCE: ExecutionContext | None = None
_INSTANCE_LOCK = threading.Lock()


def get_context() -> ExecutionContext:
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = ExecutionContext()
    return _INSTANCE


def reset_context() -> None:
    get_context().reset()
