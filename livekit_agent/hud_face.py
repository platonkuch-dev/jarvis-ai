"""FacePanel: Jarvis's face sliding out from under the status bar while he talks.

Driven by two things:
  * hud_bar.py tells it whether the agent is "speaking" (the 150 ms status poll);
  * lipsync_bridge.Receiver hands it the loudness/sibilance of the audio the
    sound card is actually playing, ~every 20-100 ms -- that moves the lips,
    and its first packets also pop the face out before the slow status poll
    catches up.

Everything is painted with QPainter (no image assets), in the bar's own
Minimal Glass palette: a dark glass panel, a soft halo that breathes with the
voice, glowing capsule eyes that blink and glance around, and a mouth whose
opening follows the loudness and whose shape follows the sound -- wide with
teeth on "с/ш/и", round and tall on loud open vowels.

The pop-out is a damped spring on one number (0 = tucked under the bar, 1 =
fully out): the face slides down from behind the bar's lower edge, clipped to
the window so it reads as emerging from the bar, with a touch of overshoot.
"""

from __future__ import annotations

import math
import random
import time
from collections import deque

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient
from PyQt6.QtWidgets import QApplication, QWidget

import lipsync_bridge
from ui_colors import C, qcol

FACE_W, FACE_H = 236, 214
_GAP = 8                     # below the bar
_AUDIO_LATENCY_S = 0.07      # packet is sent as the block goes to the sound card; heard ~this much later
_HOLD_AFTER_SPEECH_S = 0.9   # linger a moment so short pauses between sentences don't flap it
_TICK_MS = 16


class FacePanel(QWidget):
    def __init__(self, anchor, parent=None):
        """anchor() -> (x_center, y_top) in screen coordinates: where the face hangs from."""
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(FACE_W, FACE_H)
        self._anchor = anchor

        self._rx = lipsync_bridge.Receiver()
        self._queue: deque[tuple[float, float, float]] = deque()
        self._speaking = False
        self._last_audio = 0.0

        # spring-driven reveal
        self._out = 0.0
        self._out_vel = 0.0

        # mouth / expression state (smoothed)
        self._level = 0.0        # raw target from audio
        self._sib = 0.0
        self._open = 0.0         # smoothed mouth opening
        self._wide = 0.0         # smoothed sibilance -> width/teeth
        self._energy = 0.0       # slow loudness average: halo, brows, bob
        self._t = 0.0

        # eyes
        self._blink = 0.0        # 0 open .. 1 shut
        self._next_blink = time.monotonic() + random.uniform(1.5, 4.0)
        self._blink_start = None
        self._gaze = QPointF(0, 0)
        self._gaze_target = QPointF(0, 0)
        self._next_glance = time.monotonic() + random.uniform(0.8, 2.5)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._step)
        self._timer.start(_TICK_MS)

    # ------------------------------------------------------------------ inputs

    def set_speaking(self, speaking: bool) -> None:
        self._speaking = speaking

    def _pull_audio(self, now: float) -> None:
        for packet in self._rx.drain():
            self._queue.append(packet)
        # apply every packet whose sound should be audible by now; keep the newest
        applied = None
        while self._queue and self._queue[0][2] + _AUDIO_LATENCY_S <= now:
            applied = self._queue.popleft()
        if len(self._queue) > 64:       # clock hiccup: never lag behind by more than a moment
            applied = self._queue[-1]
            self._queue.clear()
        if applied is not None:
            self._level, self._sib = applied[0], applied[1]
            if self._level > 0.03:
                self._last_audio = now
        elif now - self._last_audio > 0.25:
            self._level *= 0.6          # sender went quiet (no packets while nothing plays)

    # ------------------------------------------------------------------ animation

    def _step(self) -> None:
        now = time.monotonic()
        self._pull_audio(now)
        active = self._speaking or (now - self._last_audio) < _HOLD_AFTER_SPEECH_S
        target = 1.0 if active else 0.0

        # damped spring: a little overshoot on the way out, none worth noticing on the way in
        k, damping = (0.075, 0.74) if target else (0.10, 0.62)
        self._out_vel = (self._out_vel + (target - self._out) * k) * damping
        self._out += self._out_vel

        if not active and self._out < 0.01 and abs(self._out_vel) < 0.002:
            if self.isVisible():
                self.hide()
            self._out, self._out_vel = 0.0, 0.0
            return
        if not self.isVisible():
            x, y = self._anchor()
            self.move(int(x - FACE_W / 2), int(y + _GAP))
            self.show()
        else:
            x, y = self._anchor()
            self.move(int(x - FACE_W / 2), int(y + _GAP))

        # lips: fast attack, softer release, so syllables read as separate movements
        tgt_open = self._level if active else 0.0
        self._open += (tgt_open - self._open) * (0.55 if tgt_open > self._open else 0.28)
        self._wide += (self._sib * min(1.0, self._level * 3) - self._wide) * 0.35
        self._energy += (self._level - self._energy) * 0.06
        self._t += _TICK_MS / 1000

        self._step_eyes(now)
        self.update()

    def _step_eyes(self, now: float) -> None:
        if self._blink_start is None and now >= self._next_blink:
            self._blink_start = now
        if self._blink_start is not None:
            p = (now - self._blink_start) / 0.16
            self._blink = math.sin(min(1.0, p) * math.pi)
            if p >= 1.0:
                self._blink_start, self._blink = None, 0.0
                # sometimes a quick double blink
                self._next_blink = now + (0.18 if random.random() < 0.15 else random.uniform(2.2, 5.5))
        if now >= self._next_glance:
            self._gaze_target = QPointF(random.uniform(-4.5, 4.5), random.uniform(-2.5, 2.0))
            if random.random() < 0.45:
                self._gaze_target = QPointF(0, 0)      # back to looking at the user
            self._next_glance = now + random.uniform(0.7, 2.6)
        self._gaze += (self._gaze_target - self._gaze) * 0.18

    # ------------------------------------------------------------------ painting

    def paintEvent(self, _) -> None:
        if self._out <= 0.001:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = float(self.width()), float(self.height())
        out = self._out

        # emerge from behind the bar: everything slides down from above the window's top edge
        p.setClipRect(QRectF(0, 0, w, h))
        p.translate(0, -(1.0 - min(out, 1.0)) * (h + 6))
        p.setOpacity(max(0.0, min(1.0, out * 1.6)))

        self._paint_panel(p, w, h)
        bob = -3.0 * self._energy + 1.2 * math.sin(self._t * 1.7)
        p.save()
        p.translate(w / 2, h / 2 + bob)
        p.rotate(1.2 * math.sin(self._t * 0.9) + 2.0 * (self._energy - 0.3) * math.sin(self._t * 3.1))
        p.translate(-w / 2, -h / 2)
        self._paint_eyes(p, w, h)
        self._paint_mouth(p, w, h)
        p.restore()
        self._paint_scanlines(p, w, h)

    def _paint_panel(self, p: QPainter, w: float, h: float) -> None:
        radius = 26.0
        body = QPainterPath()
        body.addRoundedRect(QRectF(1.5, 1.5, w - 3, h - 3), radius, radius)

        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0.0, qcol(C.PANEL2, 252))
        grad.setColorAt(1.0, qcol(C.BG, 255))
        p.fillPath(body, grad)

        # halo behind the face, breathing with the voice
        cx, cy = w / 2, h * 0.52
        r = 70 + 26 * self._energy + 3 * math.sin(self._t * 2.2)
        halo = QRadialGradient(QPointF(cx, cy), r)
        halo.setColorAt(0.0, qcol(C.PRI, int(40 + 70 * self._energy)))
        halo.setColorAt(0.55, qcol(C.PRI, int(14 + 30 * self._energy)))
        halo.setColorAt(1.0, qcol(C.PRI, 0))
        p.save()
        p.setClipPath(body)
        p.fillRect(QRectF(0, 0, w, h), halo)

        # thin reactor ring
        p.setPen(QPen(qcol(C.PRI, int(35 + 60 * self._energy)), 1.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        ring = 86 + 8 * self._energy
        p.drawEllipse(QPointF(cx, cy), ring, ring * 0.92)
        p.restore()

        p.setPen(QPen(qcol(C.PRI, int(110 + 120 * self._energy)), 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(body)

    def _glow_path(self, p: QPainter, path: QPainterPath, color: str, width: float, strength: float) -> None:
        for spread, alpha in ((width + 9, 18), (width + 5, 34), (width + 2, 70)):
            p.setPen(QPen(qcol(color, int(alpha * strength)), spread, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.drawPath(path)
        p.setPen(QPen(qcol(color, 255), width, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.drawPath(path)

    def _paint_eyes(self, p: QPainter, w: float, h: float) -> None:
        eye_y = h * 0.38
        eye_w, eye_h = 30.0, 38.0
        # slightly narrower when loud: reads as an animated, engaged face
        squint = 0.18 * max(0.0, self._energy - 0.35)
        open_h = max(3.0, eye_h * (1.0 - self._blink) * (1.0 - squint))
        brow_lift = 5.0 * self._energy + 2.0 * math.sin(self._t * 1.3)

        for side in (-1, 1):
            cx = w / 2 + side * 44
            rect = QRectF(cx - eye_w / 2, eye_y - open_h / 2, eye_w, open_h)
            eye = QPainterPath()
            eye.addRoundedRect(rect, eye_w / 2, min(eye_w, open_h) / 2)

            p.setPen(Qt.PenStyle.NoPen)
            for grow, alpha in ((10, 22), (6, 40), (3, 70)):
                g = QPainterPath()
                g.addRoundedRect(rect.adjusted(-grow, -grow, grow, grow), eye_w / 2 + grow, min(eye_w, open_h) / 2 + grow)
                p.fillPath(g, qcol(C.PRI, alpha))
            fill = QLinearGradient(0, rect.top(), 0, rect.bottom())
            fill.setColorAt(0.0, qcol(C.WHITE, 255))
            fill.setColorAt(1.0, qcol(C.PRI, 255))
            p.fillPath(eye, fill)

            if open_h > 10:
                # pupil: a darker glint that follows the gaze
                px = cx + self._gaze.x()
                py = eye_y + self._gaze.y() * (open_h / eye_h)
                pr = 6.5
                p.save()
                p.setClipPath(eye)
                p.setBrush(qcol(C.PRI_GHO, 235))
                p.drawEllipse(QPointF(px, py), pr, pr * min(1.0, open_h / 22))
                p.setBrush(qcol(C.WHITE, 230))
                p.drawEllipse(QPointF(px - 2.2, py - 2.6), 1.9, 1.9)
                p.restore()

            brow = QPainterPath()
            by = eye_y - eye_h / 2 - 11 - brow_lift
            brow.moveTo(cx - 15, by + 3 + side * 0.0)
            brow.quadTo(cx, by - 3, cx + 15, by + 3)
            self._glow_path(p, brow, C.PRI, 2.4, 0.6)

    def _paint_mouth(self, p: QPainter, w: float, h: float) -> None:
        cx, cy = w / 2, h * 0.73
        o = max(0.0, min(1.0, self._open))
        wide = max(0.0, min(1.0, self._wide))
        roundness = o * (1.0 - wide)            # loud and not sibilant -> "о/а"

        mw = 58 + 16 * wide - 16 * roundness     # mouth width
        mh = 2 + 34 * o * (1.0 - 0.55 * wide)    # opening height
        smile = 4.0 * (1.0 - o)                  # corners lift a bit when the mouth is closed

        left, right = QPointF(cx - mw / 2, cy - smile), QPointF(cx + mw / 2, cy - smile)
        top_y = cy - mh * 0.45
        bot_y = cy + mh * 0.62

        outer = QPainterPath(left)
        # upper lip with a small cupid's bow
        outer.cubicTo(QPointF(cx - mw * 0.36, top_y - 3), QPointF(cx - mw * 0.14, top_y - 5), QPointF(cx, top_y - 1.5))
        outer.cubicTo(QPointF(cx + mw * 0.14, top_y - 5), QPointF(cx + mw * 0.36, top_y - 3), right)
        # lower lip
        outer.cubicTo(QPointF(cx + mw * 0.30, bot_y + 5), QPointF(cx - mw * 0.30, bot_y + 5), left)
        outer.closeSubpath()

        if mh < 4.5:
            # closed: a single soft smile line
            line = QPainterPath(left)
            line.cubicTo(QPointF(cx - mw * 0.25, cy + 4), QPointF(cx + mw * 0.25, cy + 4), right)
            self._glow_path(p, line, C.PRI, 2.6, 0.9)
            return

        # inner mouth
        p.setPen(Qt.PenStyle.NoPen)
        p.fillPath(outer, qcol(C.DARK, 250))
        p.save()
        p.setClipPath(outer)
        # tongue glow at the bottom on open vowels
        tongue = QRadialGradient(QPointF(cx, bot_y + 4), mw * 0.45)
        tongue.setColorAt(0.0, qcol(C.ACC, int(90 * roundness + 20)))
        tongue.setColorAt(1.0, qcol(C.ACC, 0))
        p.fillRect(QRectF(cx - mw, cy, mw * 2, mh + 10), tongue)
        # upper teeth, most visible on "с/з/ш/и"
        teeth_h = 3 + 6 * wide
        teeth_alpha = int(110 + 130 * wide)
        teeth = QPainterPath()
        teeth.addRoundedRect(QRectF(cx - mw * 0.36, top_y - 2, mw * 0.72, teeth_h + 2), 3, 3)
        p.fillPath(teeth, qcol(C.WHITE, teeth_alpha))
        p.setPen(QPen(qcol(C.PANEL, int(teeth_alpha * 0.5)), 1))
        for i in range(1, 6):
            tx = cx - mw * 0.36 + i * mw * 0.12
            p.drawLine(QPointF(tx, top_y - 1), QPointF(tx, top_y + teeth_h))
        p.restore()

        p.setBrush(Qt.BrushStyle.NoBrush)
        self._glow_path(p, outer, C.PRI, 2.4, 0.8 + 0.4 * o)

    def _paint_scanlines(self, p: QPainter, w: float, h: float) -> None:
        p.save()
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(1.5, 1.5, w - 3, h - 3), 26, 26)
        p.setClipPath(clip)
        p.setPen(QPen(QColor(255, 255, 255, 9), 1))
        offset = (self._t * 18) % 4
        y = offset
        while y < h:
            p.drawLine(QPointF(0, y), QPointF(w, y))
            y += 4
        # one slow bright sweep
        sweep_y = (self._t * 55) % (h + 60) - 30
        g = QLinearGradient(0, sweep_y - 18, 0, sweep_y + 18)
        g.setColorAt(0.0, qcol(C.PRI, 0))
        g.setColorAt(0.5, qcol(C.PRI, 22))
        g.setColorAt(1.0, qcol(C.PRI, 0))
        p.fillRect(QRectF(0, sweep_y - 18, w, 36), g)
        p.restore()


def demo() -> None:
    """python hud_face.py -- shows the face talking to a fake signal, no worker needed."""
    import sys

    from compact_bar import CompactBar, _HEIGHT, _TOP_MARGIN
    import ui_fonts

    app = QApplication.instance() or QApplication(sys.argv)
    ui_fonts.load_bundled_fonts()
    bar = CompactBar()
    bar._show_sig.emit()
    bar._state_sig.emit("SPEAKING")

    def anchor():
        return bar.x() + bar.width() / 2, bar.y() + _HEIGHT

    face = FacePanel(anchor)
    face.set_speaking(True)
    sender = lipsync_bridge.Sender()
    start = time.monotonic()

    def fake_voice():
        t = time.monotonic() - start
        if t > 12:
            face.set_speaking(False)
            bar._state_sig.emit("LISTENING")
            return
        syllable = max(0.0, math.sin(t * 2 * math.pi * 3.8)) ** 0.6
        phrase = 1.0 if (t % 4.0) < 3.0 else 0.0
        sib = 1.0 if (int(t * 3.8) % 5 == 2) else 0.1
        sender.send(0.85 * syllable * phrase, sib)

    feeder = QTimer()
    feeder.timeout.connect(fake_voice)
    feeder.start(20)
    QTimer.singleShot(16000, app.quit)
    app.exec()


if __name__ == "__main__":
    demo()
