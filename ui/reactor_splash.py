"""
ReactorSplash: the arc-reactor style startup splash screen.

Split out of ui.py (Stage 2 module split, see REWORK_PLAN.md) with no
behavior changes.
"""
from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QFont, QLinearGradient, QPainter, QPen, QRadialGradient
from PyQt6.QtWidgets import QApplication, QWidget

from ui import fonts as _fonts
from ui.colors import C, qcol


class ReactorSplash(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(640, 640)
        self._angle = 0.0
        self._phase = 0.0
        self._assembly = 0.0
        self._core_pulse = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._step)
        self._timer.start(16)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(
            (screen.width() - self.width()) // 2,
            (screen.height() - self.height()) // 2,
        )

    @staticmethod
    def _ease(t: float) -> float:
        return t * t * (3.0 - 2.0 * t)

    @staticmethod
    def _clamp(t: float, mn: float = 0.0, mx: float = 1.0) -> float:
        return max(mn, min(mx, t))

    def _step(self):
        self._angle = (self._angle + 1.4) % 360
        self._phase = (self._phase + 0.035) % (math.pi * 2)
        self._assembly = self._clamp(self._assembly + 0.006)
        self._core_pulse = 0.88 + 0.12 * math.sin(self._phase * 2.0)
        self.update()

    def _piece_progress(self, offset: float, duration: float = 0.28) -> float:
        return self._ease(self._clamp((self._assembly - offset) / duration))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        r = min(w, h) * 0.42

        bg = QRadialGradient(cx, cy, r * 1.45)
        bg.setColorAt(0.0, qcol(C.PRI, 200))
        bg.setColorAt(0.4, qcol(C.BG, 80))
        bg.setColorAt(1.0, qcol(C.BG, 230))
        p.fillRect(self.rect(), bg)

        side_fade = QLinearGradient(0, 0, w, 0)
        side_fade.setColorAt(0.0, qcol(C.BG, 0))
        side_fade.setColorAt(0.3, qcol(C.BG, 48))
        side_fade.setColorAt(0.7, qcol(C.BG, 48))
        side_fade.setColorAt(1.0, qcol(C.BG, 0))
        p.fillRect(self.rect(), side_fade)

        # constructor grid lines
        p.setPen(QPen(qcol(C.PRI_GHO, 64), 1))
        for i in range(0, w, 52):
            p.drawLine(i, 0, i, h)
        for i in range(0, h, 52):
            p.drawLine(0, i, w, i)

        # assembly pulse ring
        ring_alpha = int(150 + 105 * self._assembly)
        ring_pen = QPen(qcol(C.PRI, ring_alpha), 16)
        ring_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(ring_pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(cx - r, cy - r, r * 2, r * 2),
                  int((self._angle - 24) * 16), int(312 * 16))

        # outer assembly segments
        seg_count = 12
        out_r = r * 0.86
        for i in range(seg_count):
            progress = self._piece_progress(i * 0.035, 0.26)
            if progress <= 0.001:
                continue
            angle = self._angle + i * (360.0 / seg_count)
            offset = 1.0 - progress
            start_r = out_r + offset * r * 0.35
            alpha = int(220 * progress)
            pen = QPen(qcol(C.ACC if i % 2 else C.PRI, alpha), 7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawArc(QRectF(cx - start_r, cy - start_r, start_r * 2, start_r * 2),
                      int((angle - 16) * 16), int(18 * 16))

        # rotating support struts that build in
        for i in range(6):
            prog = self._piece_progress(0.12 + i * 0.05, 0.30)
            if prog <= 0.001:
                continue
            ang = math.radians(i * 60 + self._angle * 0.95)
            inner_r = r * 0.26
            outer_r = r * 0.92
            mid = r * 0.62
            start = QPointF(cx + math.cos(ang) * (inner_r + (mid - inner_r) * (1.0 - prog)),
                            cy + math.sin(ang) * (inner_r + (mid - inner_r) * (1.0 - prog)))
            end = QPointF(cx + math.cos(ang) * (outer_r + (r * 0.08) * (1.0 - prog)),
                          cy + math.sin(ang) * (outer_r + (r * 0.08) * (1.0 - prog)))
            p.setPen(QPen(qcol(C.PRI, int(190 * prog)), 3))
            p.drawLine(start, end)

        # inner segmented ring assembly
        inner_r = r * 0.50
        for i in range(seg_count):
            prog = self._piece_progress(0.08 + i * 0.03, 0.26)
            if prog <= 0.001:
                continue
            angle = self._angle * 1.25 + i * (360.0 / seg_count)
            piece_r = inner_r - (1.0 - prog) * r * 0.18
            length = 14 + 6 * math.sin(math.radians(angle * 1.4 + self._phase * 42))
            alpha = int(220 * prog)
            p.setPen(QPen(qcol(C.PRI if i % 2 == 0 else C.ACC, alpha), 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(QRectF(cx - piece_r, cy - piece_r, piece_r * 2, piece_r * 2),
                      int((angle - 10) * 16), int(length * 16))

        # center core and depth rings
        core_r = r * 0.24 * (0.75 + self._core_pulse * 0.25)
        core_depth = QRadialGradient(cx, cy, r * 0.3)
        core_depth.setColorAt(0.0, qcol(C.ACC2, 255))
        core_depth.setColorAt(0.4, qcol(C.PRI, 200))
        core_depth.setColorAt(1.0, qcol(C.BG, 0))
        p.setBrush(core_depth)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QRectF(cx - core_r, cy - core_r, core_r * 2, core_r * 2))

        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(qcol(C.WHITE, int(210 * self._assembly)), 3))
        p.drawEllipse(QRectF(cx - core_r * 0.58, cy - core_r * 0.58, core_r * 1.16, core_r * 1.16))

        # core beam slices
        p.setPen(QPen(qcol(C.ACC2, int(180 * self._assembly)), 2))
        for i in range(5):
            ang = math.radians(i * 72 + self._angle * 0.8)
            start = QPointF(cx + math.cos(ang) * core_r * 0.65, cy + math.sin(ang) * core_r * 0.65)
            end = QPointF(cx + math.cos(ang) * r * 0.72, cy + math.sin(ang) * r * 0.72)
            p.drawLine(start, end)

        # highlight ring and 3D sheen
        sheen = QRadialGradient(cx, cy, r * 0.18)
        sheen.setColorAt(0.0, qcol(C.WHITE, int(180 * self._assembly)))
        sheen.setColorAt(0.4, qcol(C.ACC, int(130 * self._assembly)))
        sheen.setColorAt(1.0, qcol(C.BG, 0))
        p.setBrush(sheen)
        p.drawEllipse(QRectF(cx - r * 0.16, cy - r * 0.16, r * 0.32, r * 0.32))

        # assembly progress text
        percent = int(self._assembly * 100)
        p.setPen(QPen(qcol(C.WHITE, 220), 1))
        p.setFont(QFont(_fonts.MONO_FONT, 14, QFont.Weight.Bold))
        p.drawText(QRectF(0, cy + r * 0.64, w, 30), Qt.AlignmentFlag.AlignCenter,
                   "MARK XLVIII ARC REACTOR")
        p.setFont(QFont(_fonts.MONO_FONT, 10, QFont.Weight.Normal))
        p.setPen(QPen(qcol(C.TEXT_DIM, 200), 1))
        p.drawText(QRectF(0, cy + r * 0.74, w, 20), Qt.AlignmentFlag.AlignCenter,
                   f"ASSEMBLING CORE — {percent}%")
