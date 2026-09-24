"""Two small JSON-file channels between the worker process (tools/camera.py)
and hud_bar.py's CameraPanel -- same idiom as hud_bridge.py (see that file's
docstring for why polling a file beats a socket here), just two-way instead
of one-way: worker.py asks the HUD process to open/capture/close the camera
(it owns the actual QCamera -- see hud_bar.py), and the HUD process reports
back what happened.

Each command carries a `seq` that increments on every call. The HUD only
acts on a seq it hasn't seen yet (ignores a stale/duplicate re-read between
polls); the worker only accepts a result whose `seq` matches the command it
is currently waiting on, so a slow leftover result from a previous request
can never be mistaken for the answer to this one.
"""

from __future__ import annotations

import json
import time
from typing import Any, Literal

import config

COMMAND_PATH = config.DATA_DIR / "camera_command.json"
RESULT_PATH = config.DATA_DIR / "camera_result.json"
# Which camera to prefer when more than one is plugged in -- written by the
# panel's "Камера" tab, read fresh on every request_open() call (not cached),
# so a change there takes effect on the very next "look at the camera"
# without restarting anything.
DEVICE_PREF_PATH = config.DATA_DIR / "camera_device.json"

Action = Literal["open", "capture", "close", "list_devices"]
Status = Literal["opened", "no_camera", "captured", "closed", "error", "devices"]


def _write(path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _read(path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_command(seq: int, action: Action) -> None:
    _write(COMMAND_PATH, {"seq": seq, "action": action, "at": time.time()})


def read_command() -> dict[str, Any] | None:
    return _read(COMMAND_PATH)


def write_result(
    seq: int,
    status: Status,
    path: str | None = None,
    message: str | None = None,
    devices: list[dict[str, str]] | None = None,
) -> None:
    _write(RESULT_PATH, {
        "seq": seq, "status": status, "path": path, "message": message, "devices": devices, "at": time.time(),
    })


def read_result() -> dict[str, Any] | None:
    return _read(RESULT_PATH)


def read_preferred_device_id() -> str | None:
    """The hex-encoded QCameraDevice.id() to prefer, or None for "just use
    the first camera Qt finds" -- CameraPanel.request_open() falls back to
    that automatically if this device isn't currently plugged in."""
    data = _read(DEVICE_PREF_PATH)
    return (data or {}).get("device_id") or None


def write_preferred_device(device_id: str, description: str) -> None:
    _write(DEVICE_PREF_PATH, {"device_id": device_id, "description": description})


def clear_preferred_device() -> None:
    _write(DEVICE_PREF_PATH, {"device_id": None, "description": None})
