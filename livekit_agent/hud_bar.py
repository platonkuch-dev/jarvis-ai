"""Entry point for the compact status bar (CompactBar, ported from the
original Jarvis desktop app's ui/compact_bar.py -- see that file's docstring).

Runs as its own process (spawned by app.py alongside worker.py console) and
polls hud_bridge.STATE_PATH -- written by worker.py, a different process --
translating our lowercase status vocabulary (idle/listening/thinking/
speaking/sleeping) into CompactBar's own (LISTENING/THINKING/SPEAKING;
IDLE maps onto LISTENING's rest-state visuals, since this agent listens
continuously rather than being wake-word-gated). On "sleeping" the bar
hides entirely -- there's nothing to show while muted -- and reappears the
moment status moves off "sleeping" again (i.e. on F10 wake).

Run directly to see it on screen:
    python hud_bar.py
"""

from __future__ import annotations

import sys
import time

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

import camera_bridge
import config
import hud_bridge
import screen_watch_bridge
import ui_fonts
from compact_bar import CameraPanel, CompactBar

POLL_MS = 150
# Safety net: worker.py's playback_finished hook normally clears "speaking"
# back to "listening" the moment the agent stops talking, and "thinking"
# normally resolves in a few seconds once a reply lands. If either gets
# stuck (an interrupted utterance, an LLM error with no reply), don't leave
# the bar lying about the agent's state forever.
STUCK_STATE_TIMEOUT_S = 20.0

_STATUS_TO_BAR_STATE = {
    "idle": "LISTENING",
    "listening": "LISTENING",
    "thinking": "THINKING",
    "speaking": "SPEAKING",
    "sleeping": "SLEEPING",
}


def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)
    ui_fonts.load_bundled_fonts()

    bar = CompactBar()
    bar._show_sig.emit()

    def poll() -> None:
        state = hud_bridge.read_state()
        status = state.get("status", "idle")
        if status == "sleeping":
            bar._hide_sig.emit()
            return
        updated_at = state.get("updated_at", 0.0)
        if status in ("thinking", "speaking") and time.time() - updated_at > STUCK_STATE_TIMEOUT_S:
            status = "listening"
        if not bar.isVisible():
            bar._show_sig.emit()
        bar._state_sig.emit(_STATUS_TO_BAR_STATE.get(status, "LISTENING"))
        bar._watch_sig.emit(bool(screen_watch_bridge.read_state().get("watching")))

    timer = QTimer()
    timer.timeout.connect(poll)
    timer.start(POLL_MS)

    # Second, independent bridge for tools/camera.py -- see camera_bridge.py's
    # docstring. A plain module-level counter (not nonlocal-captured state)
    # would work too, but a mutable holder keeps poll_camera a normal nested
    # function without a `nonlocal` declaration.
    camera_panel = CameraPanel()
    _camera_seen = {"seq": 0}

    def poll_camera() -> None:
        cmd = camera_bridge.read_command()
        if cmd is None or cmd.get("seq") == _camera_seen["seq"]:
            return
        _camera_seen["seq"] = cmd["seq"]
        seq, action = cmd["seq"], cmd.get("action")

        if action == "open":
            status, message = camera_panel.request_open()
            camera_bridge.write_result(seq, status, message=message)
        elif action == "capture":
            fname = str(config.CAMERA_CAPTURES_DIR / f"frame_{seq}.jpg")

            def _done(success: bool, info: str, _seq=seq) -> None:
                if success:
                    camera_bridge.write_result(_seq, "captured", path=info)
                else:
                    camera_bridge.write_result(_seq, "error", message=info)

            camera_panel.capture(fname, _done)
        elif action == "close":
            camera_panel.request_close()
            camera_bridge.write_result(seq, "closed")
        elif action == "list_devices":
            camera_bridge.write_result(seq, "devices", devices=camera_panel.list_devices())

    camera_timer = QTimer()
    camera_timer.timeout.connect(poll_camera)
    camera_timer.start(120)

    app.exec()


if __name__ == "__main__":
    main()
