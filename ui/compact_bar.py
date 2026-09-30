"""
CompactBar: the small always-on-top pill shown only while JARVIS is
actually listening/thinking/speaking -- the visible counterpart to the
wake-word-gated session in main.py (`_require_wake_word`).

Deliberately NOT a shrunk-down HudCanvas: HudCanvas (ui/hud_canvas.py) has
a hard 300x300 minimum and a subsystem-constellation layout built for a
big square face, both wrong for a slim bar. This is a new, much simpler
widget driven by the same state vocabulary every other JARVIS surface
already uses (SPEAKING / THINKING / LISTENING / SLEEPING -- see
MainWindow._apply_state), so it stays visually consistent without
reusing HudCanvas's particle system.
"""
from __future__ import annotations

import math

from PyQt6.QtCore import QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QApplication, QWidget

from ui import fonts as _fonts
from ui.colors import C, qcol

_WIDTH, _HEIGHT = 280, 64
_TOP_MARGIN = 28

# (energy, warmth) targets -- same two-axis idea as HudCanvas's
# _STATE_TARGETS, kept as its own small copy here rather than importing
# hud_canvas internals, since this widget's render loop is intentionally
# much simpler (a handful of bars, not a particle field).
_STATE_TARGETS = {
    "SPEAKING":  (0.95, 0.90),
    "THINKING":  (0.55, 0.32),
    "LISTENING": (0.40, 0.08),
    "SLEEPING":  (0.10, 0.02),
}
_DEFAULT_TARGET = (0.30, 0.10)

_BAR_COUNT = 5


class CompactBar(QWidget):
    # JarvisUI (ui/jarvis_ui.py) is called from arbitrary threads (the
    # asyncio loop thread in main.py, worker threads from
    # main_window.py's on_text_command handlers, ...) -- exactly like
    # MainWindow's _state_sig, these signals are the thread-safe entry
    # point; connected to plain-method slots below, never called directly
    # off the Qt thread.
    _show_sig  = pyqtSignal()
    _hide_sig  = pyqtSignal()
    _state_sig = pyqtSignal(str)

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

        self._show_sig.connect(self._show_bar)
        self._hide_sig.connect(self.hide)
        self._state_sig.connect(self._set_state)

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

        label = self.state.capitalize() if self.state else "Jarvis"
        p.setPen(QPen(qcol(C.TEXT, 235), 1))
        p.setFont(QFont(_fonts.UI_FONT, 10, QFont.Weight.DemiBold))
        text_x = start_x + total_w + 14
        p.drawText(
            QRectF(text_x, 0, w - text_x - 14, h),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            label,
        )
