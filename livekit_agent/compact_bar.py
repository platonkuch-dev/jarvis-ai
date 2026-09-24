"""
CompactBar: the small always-on-top pill showing the agent's current state.

Ported from the original Jarvis desktop app's ui/compact_bar.py, unchanged
in visual design and animation -- only the import paths differ (this
project's ui_colors.py/ui_fonts.py instead of the ui/ package) and the
state vocabulary gained "IDLE" (mapped to the same rest-state visuals as
LISTENING; see hud_bar.py, which drives this widget from hud_bridge.py's
polled state file instead of the original's direct Qt signal calls from
main.py).
"""
from __future__ import annotations

import math

from PyQt6.QtCore import QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QPainter, QPainterPath, QPen
from PyQt6.QtMultimedia import QCamera, QImageCapture, QMediaCaptureSession, QMediaDevices
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtWidgets import QApplication, QWidget

import camera_bridge
import config
import ui_fonts as _fonts
from ui_colors import C, qcol

_WIDTH, _HEIGHT = 280, 64
_TOP_MARGIN = 28

# (energy, warmth) targets -- two-axis animation drive, same idea as the
# original HudCanvas's _STATE_TARGETS.
_STATE_TARGETS = {
    "SPEAKING":  (0.95, 0.90),
    "THINKING":  (0.55, 0.32),
    "LISTENING": (0.40, 0.08),
    "SLEEPING":  (0.10, 0.02),
}
_DEFAULT_TARGET = (0.30, 0.10)

_BAR_COUNT = 5


class CompactBar(QWidget):
    # Signals are the thread-safe entry point: worker.py's process writes
    # hud_bridge's state file from a different OS process entirely, so
    # hud_bar.py's QTimer poller (running on the Qt thread already) just
    # calls these directly -- the signal/slot indirection is kept anyway
    # to match the original's threading contract exactly.
    _show_sig  = pyqtSignal()
    _hide_sig  = pyqtSignal()
    _state_sig = pyqtSignal(str)
    _watch_sig = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(_WIDTH, _HEIGHT)

        self.state = "LISTENING"
        self._energy = 0.0
        self._warmth = 0.0
        self._phase = 0.0
        self._bar_phases = [i * 0.9 for i in range(_BAR_COUNT)]
        self._watching = False   # tools/screen_watch.py, via screen_watch_bridge.py

        self._show_sig.connect(self._show_bar)
        self._hide_sig.connect(self.hide)
        self._state_sig.connect(self._set_state)
        self._watch_sig.connect(self._set_watching)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._step)
        self._timer.start(33)  # ~30fps -- plenty for a small indicator, cheap while idle

    def _show_bar(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        self.move((screen.width() - _WIDTH) // 2, screen.y() + _TOP_MARGIN)
        self.show()
        self.raise_()

    def _set_state(self, state: str) -> None:
        self.state = state

    def _set_watching(self, watching: bool) -> None:
        self._watching = watching

    @staticmethod
    def _ease_toward(current: float, target: float, rate: float = 0.12) -> float:
        return current + (target - current) * rate

    def _step(self) -> None:
        target_energy, target_warmth = _STATE_TARGETS.get(self.state, _DEFAULT_TARGET)
        self._energy = self._ease_toward(self._energy, target_energy)
        self._warmth = self._ease_toward(self._warmth, target_warmth)
        self._phase = (self._phase + 0.12) % (math.pi * 2)
        if self.isVisible():
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        glow_color = qcol(C.ACC if self._warmth > 0.5 else C.PRI, int(90 + 120 * self._energy))
        path = QPainterPath()
        path.addRoundedRect(QRectF(1, 1, w - 2, h - 2), h / 2, h / 2)
        p.fillPath(path, qcol(C.PANEL, 235))
        p.setPen(QPen(glow_color, 1.5))
        p.drawPath(path)

        # Small equalizer-style indicator, height driven by _energy plus a
        # per-bar phase offset so it doesn't just pulse as one flat block.
        bar_w = 4
        gap = 6
        total_w = _BAR_COUNT * bar_w + (_BAR_COUNT - 1) * gap
        start_x = 18
        mid_y = h / 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qcol(C.ACC if self._warmth > 0.5 else C.PRI, 230))
        for i in range(_BAR_COUNT):
            wobble = 0.35 + 0.65 * abs(math.sin(self._phase + self._bar_phases[i]))
            bar_h = max(4.0, (h * 0.55) * self._energy * wobble)
            x = start_x + i * (bar_w + gap)
            p.drawRoundedRect(QRectF(x, mid_y - bar_h / 2, bar_w, bar_h), 2, 2)

        label = _state_label(self.state)
        p.setPen(QPen(qcol(C.TEXT, 235), 1))
        p.setFont(QFont(_fonts.UI_FONT, 10, QFont.Weight.DemiBold))
        text_x = start_x + total_w + 14
        p.drawText(
            QRectF(text_x, 0, w - text_x - 14, h),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            label,
        )

        if self._watching:
            # Small always-visible pulsing dot: continuous screen capture is
            # privacy-sensitive, so this stays on screen for as long as
            # tools/screen_watch.py's background loop is active -- not just a
            # one-time spoken mention when it starts.
            pulse = 0.5 + 0.5 * abs(math.sin(self._phase * 1.4))
            r = 3.5 + 1.5 * pulse
            cx, cy = w - 12, 12
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcol(C.ACC, int(140 + 100 * pulse)))
            p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))


_STATE_LABELS_RU = {
    "LISTENING": "Слушаю",
    "THINKING": "Думаю",
    "SPEAKING": "Говорю",
}


def _state_label(state: str) -> str:
    if state == "SLEEPING":
        return f"Сплю — {config.WAKE_HOTKEY.upper()}"
    return _STATE_LABELS_RU.get(state, state.capitalize() if state else "Jarvis")


_CAM_W, _CAM_H = 320, 220
_CAM_GAP = 10  # vertical gap below CompactBar


class CameraPanel(QWidget):
    """The webcam preview that pops out from under CompactBar on "look at
    the camera" -- driven by tools/camera.py through camera_bridge.py's two
    JSON files (see that module's docstring for the split: this process
    owns the QCamera, the worker process only ever asks for open/capture/
    close and reads the result back).

    The "pop out" animation is just CompactBar's own trick (_ease_toward
    applied every ~33ms tick) aimed at the window's *height* instead of a
    painted bar: growing the top-level widget from 1px to _CAM_H, anchored
    at a fixed point right below the bar, means Qt clips every child
    (video included) to that shrinking rect for free -- no masking needed.
    The live video only becomes visible once the panel is mostly open, so
    the growth phase reads as the panel unfolding rather than a squashed
    video peeking out early.
    """

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._open_amt = 0.0
        self._open_target = 0.0
        self._mode = "closed"       # closed | opening | live | no_camera | error
        self._message: str | None = None

        self._video = QVideoWidget(self)
        self._video.setGeometry(12, 12, _CAM_W - 24, _CAM_H - 24)
        self._video.hide()

        self._capture_session = QMediaCaptureSession()
        self._capture_session.setVideoOutput(self._video)
        self._camera: QCamera | None = None
        self._image_capture: QImageCapture | None = None
        self._pending_capture_cb = None

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._step)
        self._timer.start(33)

    @staticmethod
    def _anchor_pos() -> tuple[int, int]:
        screen = QApplication.primaryScreen().availableGeometry()
        x = screen.x() + (screen.width() - _CAM_W) // 2
        y = screen.y() + _TOP_MARGIN + _HEIGHT + _CAM_GAP
        return x, y

    def request_open(self) -> tuple[str, str | None]:
        """Starts the pop-in animation and tries to start the camera.
        Returns (status, message) -- status is "opened", "no_camera" or
        "error", ready to hand straight to camera_bridge.write_result()."""
        self._mode = "opening"
        self._message = None
        self._open_target = 1.0
        if not self.isVisible():
            x, y = self._anchor_pos()
            self.setGeometry(x, y, _CAM_W, 1)
            self.setWindowOpacity(0.0)
            self.show()
            self.raise_()

        devices = QMediaDevices.videoInputs()
        if not devices:
            self._mode = "no_camera"
            return "no_camera", "Камера не найдена на этом компьютере."

        chosen = devices[0]
        preferred_id = camera_bridge.read_preferred_device_id()
        if preferred_id:
            for d in devices:
                if bytes(d.id()).hex() == preferred_id:
                    chosen = d
                    break
            # else: the saved camera isn't plugged in right now -- silently
            # fall back to the first available one rather than failing.

        try:
            self._camera = QCamera(chosen)
            self._capture_session.setCamera(self._camera)
            self._image_capture = QImageCapture(self._capture_session)
            self._capture_session.setImageCapture(self._image_capture)
            self._image_capture.imageSaved.connect(self._on_image_saved)
            self._image_capture.errorOccurred.connect(self._on_image_error)
            self._camera.start()
        except Exception as exc:
            self._mode = "error"
            self._message = str(exc)
            return "error", str(exc)

        self._mode = "live"
        return "opened", None

    def capture(self, path: str, on_done) -> None:
        """on_done(success: bool, path_or_error: str) fires once the sensor
        has actually written the file (imageSaved) or failed (errorOccurred)
        -- not immediately, so callers must not assume it ran synchronously."""
        self._pending_capture_cb = on_done
        if self._image_capture is None:
            self._pending_capture_cb = None
            on_done(False, "камера не готова")
            return
        self._image_capture.captureToFile(path)

    def _on_image_saved(self, _id: int, filename: str) -> None:
        cb, self._pending_capture_cb = self._pending_capture_cb, None
        if cb:
            cb(True, filename)

    def _on_image_error(self, _id: int, _err, err_str: str) -> None:
        cb, self._pending_capture_cb = self._pending_capture_cb, None
        if cb:
            cb(False, err_str)

    @staticmethod
    def list_devices() -> list[dict[str, str]]:
        """Every video input Qt currently sees, for the panel's camera picker.
        Pure enumeration -- safe to call regardless of whether the panel is
        open or a capture is in flight."""
        return [
            {"id": bytes(d.id()).hex(), "description": d.description()}
            for d in QMediaDevices.videoInputs()
        ]

    def request_close(self) -> None:
        self._open_target = 0.0

    def stop_camera(self) -> None:
        if self._camera is not None:
            try:
                self._camera.stop()
            except Exception:
                pass
        self._camera = None
        self._image_capture = None
        try:
            self._capture_session.setCamera(None)
            self._capture_session.setImageCapture(None)
        except Exception:
            pass

    def _step(self) -> None:
        if not self.isVisible() and self._open_target <= 0.0:
            return
        rate = 0.20
        self._open_amt += (self._open_target - self._open_amt) * rate
        amt = min(1.0, max(0.0, self._open_amt))
        if self._open_target == 0.0 and amt < 0.02:
            self.stop_camera()
            self.hide()
            self._mode = "closed"
            return

        x, y = self._anchor_pos()
        h = max(1, int(_CAM_H * amt))
        self.setGeometry(x, y, _CAM_W, h)
        self.setWindowOpacity(min(1.0, amt * 1.4))
        self._video.setVisible(self._mode == "live" and amt > 0.6)
        self.update()

    def paintEvent(self, _) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        if h < 3:
            return

        radius = min(18.0, h / 2)
        path = QPainterPath()
        path.addRoundedRect(QRectF(1, 1, w - 2, h - 2), radius, radius)
        p.fillPath(path, qcol(C.PANEL, 235))
        p.setPen(QPen(qcol(C.PRI, 190), 1.5))
        p.drawPath(path)

        if self._mode in ("no_camera", "error") and self.height() > _CAM_H * 0.5:
            text = "Камера не найдена" if self._mode == "no_camera" else (self._message or "Ошибка камеры")
            p.setPen(QPen(qcol(C.TEXT_MED, 255), 1))
            p.setFont(QFont(_fonts.UI_FONT, 11, QFont.Weight.DemiBold))
            p.drawText(
                QRectF(16, 0, w - 32, h),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                text,
            )
