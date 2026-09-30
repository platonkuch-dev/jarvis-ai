"""Unit checks for graceful window-close retry behavior."""
from __future__ import annotations

from windows_control import native, router
from windows_control.context import reset_context


class FakeWindow:
    hwnd = 101
    pid = 202
    title = "Test App"
    process_name = "test.exe"

    def as_dict(self) -> dict:
        return {"hwnd": self.hwnd, "pid": self.pid, "title": self.title}


def check(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"[PASS] {label}")


def test_retry() -> None:
    originals = native.close_window, native.wait_for_window_gone, native.focus_window
    calls: list[str] = []
    try:
        reset_context()
        native.close_window = lambda _hwnd: calls.append("close") or True
        waits = iter([False, True])
        native.wait_for_window_gone = lambda _hwnd, timeout: calls.append(f"wait:{timeout}") or next(waits)
        native.focus_window = lambda _hwnd: calls.append("focus") or True
        original_target = router._target_window_info
        router._target_window_info = lambda _app: FakeWindow()
        try:
            result = router.close("Test App")
        finally:
            router._target_window_info = original_target
        check("close retries after a still-open first attempt", calls == ["close", "wait:3.0", "focus", "close", "wait:2.0"])
        check("successful retry reports close completion", result.success and "Closed Test App" in result.message)
    finally:
        native.close_window, native.wait_for_window_gone, native.focus_window = originals


test_retry()