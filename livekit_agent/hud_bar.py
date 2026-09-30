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

    if config.HUD_FACE and config.HUD_FACE_STYLE in ("core", "head3d"):
        timers = _run_head_mode()
        app.exec()
        return

    bar = CompactBar()
    bar._show_sig.emit()
    # Declared before the face so its anchor can hang it below an open camera preview.
    camera_panel = CameraPanel()
    timers = _camera_bridge(camera_panel)

    face = None
    if config.HUD_FACE:
        def face_anchor() -> tuple[float, float]:
            below = camera_panel if camera_panel.isVisible() else bar
            return bar.x() + bar.width() / 2, below.y() + below.height()

        import hud_avatar

        if config.HUD_FACE_STYLE == "humanoid":
            from hud_humanoid import HumanoidPanel

            face = HumanoidPanel(face_anchor)            # hologram from an orb, no assets needed
        elif hud_avatar.assets_ready():
            face = hud_avatar.AvatarPanel(face_anchor)   # realistic bust (scripts/make_avatar.py)
        else:
            from hud_face import FacePanel

            face = FacePanel(face_anchor)                # drawn fallback
    # The launch entrance plays once the worker writes its first fresh state
    # (it is up and about to greet), and again on every wake from sleep.
    entrance = {"seen": hud_bridge.read_state().get("updated_at", 0.0), "pending": True, "sleeping": False}

    def maybe_entrance(state: dict) -> None:
        if not hasattr(face, "play_assembly"):
            return
        fresh = state.get("updated_at", 0.0) != entrance["seen"]
        if entrance["pending"] and fresh:
            entrance["pending"] = False
            face.play_assembly()

    def poll() -> None:
        state = hud_bridge.read_state()
        status = state.get("status", "idle")
        if status == "sleeping":
            bar._hide_sig.emit()
            if face is not None:
                face.set_speaking(False)
            entrance["sleeping"] = True
            return
        if entrance["sleeping"]:
            entrance["sleeping"], entrance["pending"] = False, True
            entrance["seen"] = None
        if face is not None:
            maybe_entrance(state)
        updated_at = state.get("updated_at", 0.0)
        if status in ("thinking", "speaking") and time.time() - updated_at > STUCK_STATE_TIMEOUT_S:
            status = "listening"
        if face is not None:
            face.set_speaking(status == "speaking")
        if not bar.isVisible():
            bar._show_sig.emit()
        bar._state_sig.emit(_STATUS_TO_BAR_STATE.get(status, "LISTENING"))
        bar._watch_sig.emit(bool(screen_watch_bridge.read_state().get("watching")))

    timer = QTimer()
    timer.timeout.connect(poll)
    timer.start(POLL_MS)

    app.exec()


def _camera_bridge(camera_panel: CameraPanel) -> list[QTimer]:
    """Second, independent bridge for tools/camera.py -- see camera_bridge.py's
    docstring. Returns its timer so the caller keeps it alive."""
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
    return [camera_timer]


def _run_head_mode() -> list:
    """The 3D head replaces the bar: always on screen in the bottom-right
    corner while Jarvis runs (a standby orb until the worker is up and while
    asleep), with the status poll driving its states. Returns the objects the
    caller must keep alive."""
    from compact_bar import _CAM_H, _CAM_W

    if config.HUD_FACE_STYLE == "head3d":
        from hud_head3d import HeadPanel, WIN_W
    else:
        from hud_core import CorePanel as HeadPanel, WIN_W

    head = HeadPanel()
    camera_panel = CameraPanel()
    # the camera preview opens right above the head
    camera_panel.anchor_fn = lambda: (head.x() + WIN_W - _CAM_W, head.y() - _CAM_H - 8)
    entrance = {"seen": hud_bridge.read_state().get("updated_at", 0.0), "pending": True}

    def poll() -> None:
        state = hud_bridge.read_state()
        status = state.get("status", "idle")
        updated_at = state.get("updated_at", 0.0)
        head.set_watching(bool(screen_watch_bridge.read_state().get("watching")))
        if entrance["pending"]:
            if updated_at == entrance["seen"]:
                return                      # worker not up yet: the orb keeps pulsing ("BOOTING")
            entrance["pending"] = False
            if status != "sleeping":
                head.set_status("listening")
                head.play_assembly()
        if status in ("thinking", "speaking") and time.time() - updated_at > STUCK_STATE_TIMEOUT_S:
            status = "listening"
        head.set_status("listening" if status == "idle" else status)

    timer = QTimer()
    timer.timeout.connect(poll)
    timer.start(POLL_MS)
    return [head, camera_panel, timer] + _camera_bridge(camera_panel) + _panel_link(head)


def _panel_link(head) -> list:
    """Hosts the full-screen HUD panel (hud_panel.py) from this process -- it
    owns the live voice level the panel's face talks with -- opens it on the
    second monitor at launch (HUD_PANEL_AUTO), and hides the corner face
    while the panel is showing Jarvis, so there is one of him on screen."""
    import hud_panel

    hud_panel.serve(level_fn=lambda: (getattr(head, "_level", 0.0), getattr(head, "_sib", 0.0)))
    hud_panel.set_plan(False)       # a fresh launch shows just Jarvis; "открой план" unfolds the plan

    def open_on_second_monitor() -> None:
        try:
            import screens

            if len(screens.monitors()) >= 2 and not hud_panel.is_open():
                hud_panel.open_panel(config.HUD_PANEL_MONITOR)
        except Exception:
            pass

    if config.HUD_PANEL_AUTO:
        QTimer.singleShot(1500, open_on_second_monitor)

    def sync_visibility() -> None:
        panel_live = hud_panel.feed.clients > 0
        if panel_live and head.isVisible():
            head.hide()
        elif not panel_live and not head.isVisible():
            head.show()

    vis = QTimer()
    vis.timeout.connect(sync_visibility)
    vis.start(400)
    return [vis]


if __name__ == "__main__":
    main()
