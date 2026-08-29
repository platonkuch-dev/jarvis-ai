"""
actions/screen_watch.py — toggleable, throttled background screen watching.

While active, grabs a fresh screenshot every WATCH_INTERVAL_SECONDS and keeps
only the latest one cached in memory (nothing is written to disk, nothing is
sent anywhere on its own). This does NOT call any vision model on its own —
it just means a recent frame is already sitting in memory when the user asks
a real question, so screen_process can skip the extra capture round-trip.

Off by default. Must be explicitly started/stopped via the screen_watch tool.
"""
import threading
import time

from actions.screen_processor import _capture_screen
from core.config import SCREEN_WATCH_INTERVAL_SECONDS as WATCH_INTERVAL_SECONDS

_state = {
    "active":       False,
    "thread":       None,
    "stop_event":   None,
    "latest_bytes": None,
    "latest_mime":  None,
    "latest_time":  0.0,
}
_lock = threading.Lock()


def _capture_loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            img_bytes, mime_type = _capture_screen()
            with _lock:
                _state["latest_bytes"] = img_bytes
                _state["latest_mime"]  = mime_type
                _state["latest_time"]  = time.time()
        except Exception as e:
            print(f"[ScreenWatch] ⚠️ capture failed: {e}")
        stop_event.wait(WATCH_INTERVAL_SECONDS)


def start() -> str:
    with _lock:
        if _state["active"]:
            return "Already watching your screen."
        stop_event = threading.Event()
        thread = threading.Thread(target=_capture_loop, args=(stop_event,), daemon=True)
        _state["stop_event"] = stop_event
        _state["thread"]     = thread
        _state["active"]     = True
    try:
        thread.start()
    except Exception as e:
        with _lock:
            _state["active"] = False
        return f"Couldn't start watching your screen: {e}"
    return f"Now watching your screen — refreshing every {WATCH_INTERVAL_SECONDS} seconds until you tell me to stop."


def stop() -> str:
    with _lock:
        if not _state["active"]:
            return "I wasn't watching your screen."
        _state["stop_event"].set()
        _state["active"]       = False
        _state["latest_bytes"] = None
        _state["latest_mime"]  = None
    return "Stopped watching your screen."


def status() -> str:
    with _lock:
        if not _state["active"]:
            return "Not currently watching your screen."
        age = time.time() - _state["latest_time"]
    return f"Watching your screen — last frame captured {age:.0f}s ago, refreshing every {WATCH_INTERVAL_SECONDS}s."


def get_latest_frame() -> tuple[bytes, str] | None:
    """Returns the most recent cached (bytes, mime_type) if watching is active
    and the frame is no older than one watch cycle plus a small margin — else None."""
    with _lock:
        if not _state["active"] or _state["latest_bytes"] is None:
            return None
        if time.time() - _state["latest_time"] > WATCH_INTERVAL_SECONDS + 3:
            return None
        return _state["latest_bytes"], _state["latest_mime"]


def screen_watch(parameters: dict, response=None, player=None, session_memory=None) -> str:
    action = (parameters or {}).get("action", "status").lower().strip()
    if action == "start":
        return start()
    if action == "stop":
        return stop()
    return status()
