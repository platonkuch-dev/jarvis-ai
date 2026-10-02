"""HeadPanel: Jarvis as a volumetric holographic head in the bottom-right
corner of the screen, on screen for as long as Jarvis runs, instead of the
status bar.

The head is real 3D: points on a sculpted head surface (brow ridge, eye
sockets, nose, cheekbones, lips, chin -- offsets on an ellipsoid), laid out
as horizontal contour rings like the reference hologram, plus a neck and
shoulders. Each frame the points are deformed (jaw), rotated (the head turns,
nods and tilts on its own), lit by their normals, projected in perspective
and rendered additively with numpy:
  * silhouette glow from a fresnel term -- the outline burns, the middle is
    darker, so the volume reads without any fill;
  * the far side of the head shows through faintly (it is a hologram);
  * an orange core glows behind the face and burns yellow-white when he talks.

Talking: the live audio level (lipsync_bridge) opens the jaw -- the lower lip
and chin rings drop, the upper lip lifts a touch, sibilants stretch the
mouth wide -- and the gap between the lips shows the core's light from
inside. The eyes glow and blink.

States (from hud_bar.py's status poll):
  listening -- calm breathing, a slow scan wave rises through the rings;
  thinking  -- the scan wave runs fast and the core turns cyan-white;
  speaking  -- mouth + burning core, small nods with the voice;
  sleeping  -- the head streams back into a small standby orb.
Before the worker is up (and while asleep) only the orb pulses. The launch /
wake entrance streams the head out of the orb.
"""

from __future__ import annotations

import math
import random
import time
from collections import deque

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QRadialGradient
from PyQt6.QtWidgets import QApplication, QWidget

import lipsync_bridge

WIN_W, WIN_H = 300, 330
MARGIN_R, MARGIN_B = 14, 6
_SCALE = 70.0                   # pixels per model unit at depth 0
_TOP = 22.0                     # screen y of model y = -1.3 (top of the head)
_CAM_D = 6.0                    # camera distance (perspective strength)
ORB = np.array([WIN_W / 2, WIN_H - 22.0], dtype=np.float32)
_TICK_MS = 16
_IDLE_TICK_MS = 66   # ~15 fps once he has settled into the sleeping orb
_AUDIO_LATENCY_S = 0.07

CYAN = np.array([0.12, 0.55, 1.00], dtype=np.float32)
RIM = np.array([0.55, 0.90, 1.00], dtype=np.float32)
ORANGE = np.array([1.00, 0.45, 0.08], dtype=np.float32)
YELLOW = np.array([1.00, 0.92, 0.60], dtype=np.float32)
GOLD = np.array([1.00, 0.66, 0.22], dtype=np.float32)
WHITE = np.array([0.85, 0.97, 1.00], dtype=np.float32)

PART_HEAD, PART_NECK, PART_BODY = 0, 1, 2
K_SURF, K_EYE, K_MOUTH, K_NERVE, K_DUST = range(5)

_LIGHT = np.array([-0.45, -0.55, -0.70], dtype=np.float32)
_LIGHT /= np.linalg.norm(_LIGHT)
_PIVOT = np.array([0.0, 0.95, 0.15], dtype=np.float32)       # where the head turns: top of the neck


def _ss(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


def _g(v, m, s):
    return np.exp(-((v - m) / s) ** 2)


def _features(u, y):
    """Forward offset of the face surface (model units) at lateral u (-1..1) and height y."""
    brow = 0.12 * _g(y, -0.30, 0.09) * _g(u, 0.0, 0.55)
    sockets = -0.14 * (_g(u, -0.36, 0.13) + _g(u, 0.36, 0.13)) * _g(y, -0.12, 0.09)
    # nose: a gentle bridge off the brow rising to a rounded tip -- kept in
    # proportion with the other features (brow/cheeks/chin) so it reads as a
    # soft ridge rather than a jutting spike.
    nose_prof = 0.15 * _ss((y + 0.26) / 0.30) * (1 - _ss((y - 0.16) / 0.24)) * (y > -0.30)
    nose = nose_prof * _g(u, 0.0, 0.12 + 0.05 * _ss(y / 0.3))
    wings = 0.045 * (_g(u, -0.12, 0.06) + _g(u, 0.12, 0.06)) * _g(y, 0.27, 0.06)
    cheeks = 0.09 * (_g(u, -0.55, 0.2) + _g(u, 0.55, 0.2)) * _g(y, 0.08, 0.14)
    upper_lip = 0.11 * _g(u, 0.0, 0.21) * _g(y, 0.52, 0.05)
    lower_lip = 0.10 * _g(u, 0.0, 0.19) * _g(y, 0.665, 0.055)
    groove = -0.06 * _g(u, 0.0, 0.23) * _g(y, 0.59, 0.02)
    chin = 0.12 * _g(u, 0.0, 0.26) * _g(y, 0.98, 0.13)
    return brow + sockets + nose + wings + cheeks + upper_lip + lower_lip + groove + chin


# head profile, crown to chin: half-width and half-depth by height -- widest
# at the temples, a real jaw angle, then the chin (an ellipsoid read as a light bulb)
_HY = np.array([-1.30, -1.24, -1.12, -0.95, -0.70, -0.35, 0.00, 0.30, 0.58, 0.80, 0.98, 1.12, 1.22, 1.30])
_HX = np.array([0.00, 0.32, 0.58, 0.77, 0.91, 0.98, 0.97, 0.92, 0.86, 0.78, 0.64, 0.48, 0.34, 0.12])
_HZ = np.array([0.00, 0.42, 0.72, 0.92, 1.05, 1.10, 1.09, 1.05, 0.98, 0.90, 0.80, 0.68, 0.55, 0.30])


def _head_surface(phi, y):
    rx = np.interp(y, _HY, _HX)
    rz = np.interp(y, _HY, _HZ)
    s, c = np.sin(phi), np.cos(phi)
    x = rx * s
    z = -rz * c
    front = np.clip(c, 0, 1) ** 1.5
    z = z - _features(s, y) * front
    return x, np.broadcast_to(y, np.shape(x)).astype(np.float64), z


def _body_rx(y):
    return np.interp(y, [1.62, 1.74, 1.86, 2.0, 2.2, 2.45, 2.8], [0.50, 0.70, 1.10, 1.45, 1.70, 1.84, 1.90])


def _body_rz(y):
    return np.interp(y, [1.62, 1.9, 2.3, 2.8], [0.46, 0.50, 0.58, 0.62])


def _kernel(size: int, sigma: float):
    r = np.arange(size) - size // 2
    kx, ky = np.meshgrid(r, r)
    return kx.ravel(), ky.ravel(), np.exp(-(kx * kx + ky * ky).ravel() / (2 * sigma * sigma)).astype(np.float32)


class _Model:
    def __init__(self, seed: int = 5) -> None:
        rng = np.random.default_rng(seed)
        P, N, part, kind, base, ring = [], [], [], [], [], []

        def add(p, n, pt, kd, b, rg=0):
            p = np.asarray(p, dtype=np.float32).reshape(-1, 3)
            n = np.asarray(n, dtype=np.float32).reshape(-1, 3)
            P.append(p)
            N.append(n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-6))
            m = p.shape[0]
            part.append(np.full(m, pt, np.int8))
            kind.append(np.full(m, kd, np.int8))
            base.append(np.broadcast_to(np.asarray(b, np.float32), (m,)).copy())
            ring.append(np.full(m, rg, np.int16))

        # head: horizontal rings over the sculpted surface
        for i, y0 in enumerate(np.arange(-1.26, 1.29, 0.052)):
            x0, _, z0 = _head_surface(np.array([math.pi / 2]), y0)
            rx = max(abs(float(x0[0])), 0.05)
            # Front sampled uniformly in the lateral coordinate u = sin(phi), not
            # in phi itself: with x = rx*sin(phi), equal angular steps bunch
            # points up near the profile (phi -> +-pi/2) and go sparse right at
            # phi = 0 -- the dead center of the face, and also its
            # closest-to-camera point, so perspective magnifies that gap too.
            # That showed up as a vertical seam straight down the middle of
            # the face. Stepping u instead keeps the on-surface spacing even.
            u = np.arange(-1.0, 1.0, 0.034 / rx)
            front = np.arcsin(np.clip(u, -1.0, 1.0))
            back = np.arange(math.pi / 2, 3 * math.pi / 2, 0.075 / rx)
            phi = np.concatenate([front, back])
            x, y, z = _head_surface(phi, y0)
            d = 1e-3
            xa, ya, za = _head_surface(phi + d, y0)
            xb, yb, zb = _head_surface(phi, y0 + d)
            t1 = np.stack([xa - x, ya - y, za - z], 1)
            t2 = np.stack([xb - x, yb - y, zb - z], 1)
            n = np.cross(t2, t1)
            radial = np.stack([x, np.zeros_like(x), z], 1)
            n *= np.sign(np.sum(n * radial, 1, keepdims=True) + 1e-9)
            add(np.stack([x, y, z], 1), n, PART_HEAD, K_SURF, 1.0, i)

        # eyes: small glowing clusters just inside the sockets
        for side in (-1, 1):
            a = rng.uniform(0, 2 * math.pi, 70)
            r = np.sqrt(rng.uniform(0, 1, 70))
            u = side * 0.36 + 0.075 * r * np.cos(a)
            yy = -0.12 + 0.035 * r * np.sin(a)
            x, y, z = _head_surface(np.arcsin(np.clip(u, -1, 1)), yy)
            add(np.stack([x, y, z + 0.03], 1), np.tile([0, 0, -1], (70, 1)), PART_HEAD, K_EYE, 1.0)

        # mouth cavity light: a sheet just inside the lips, opened by the jaw at runtime
        mu, mt = np.meshgrid(np.linspace(-0.26, 0.26, 26), np.linspace(0, 1, 7))
        mu, mt = mu.ravel(), mt.ravel()
        x, y, z = _head_surface(np.arcsin(mu), 0.60)
        self.mouth_t = mt.astype(np.float32)
        add(np.stack([x, y, z + 0.07], 1), np.tile([0, 0, -1], (mu.size, 1)), PART_HEAD, K_MOUTH,
            (1 - (np.abs(mu) / 0.28) ** 2))

        # neck
        for y0 in np.arange(1.02, 1.80, 0.052):
            phi = np.arange(0, 2 * math.pi, 0.075)
            s, c = np.sin(phi), np.cos(phi)
            p = np.stack([0.50 * s, np.full_like(s, y0), 0.18 - 0.44 * c], 1)
            # hidden up under the jaw: only a faint see-through there
            add(p, np.stack([s, np.zeros_like(s), -c], 1), PART_NECK, K_SURF, 0.3 if y0 < 1.3 else 1.0)

        # gold nerves up the front of the neck
        for _ in range(4):
            ph, y0 = rng.uniform(-0.6, 0.6), rng.uniform(1.8, 1.95)
            pts = []
            while y0 > 1.18:
                pts.append((0.51 * math.sin(ph), y0, 0.18 - 0.45 * math.cos(ph)))
                y0 -= 0.012
                ph += rng.normal(0, 0.03)
            pts = np.array(pts)
            add(pts, np.tile([0, 0, -1], (len(pts), 1)), PART_NECK, K_NERVE, 0.8)

        # shoulders / upper chest: boxy cross-sections
        for y0 in np.arange(1.66, 2.80, 0.072):
            rx, rz = _body_rx(y0), _body_rz(y0)
            phi = np.arange(0, 2 * math.pi, 0.045 / max(rx, 0.3) * 1.6)
            s, c = np.sin(phi), np.cos(phi)
            x = rx * np.sign(s) * np.abs(s) ** 0.75
            z = 0.2 - rz * np.sign(c) * np.abs(c) ** 0.75
            fade = 0.8 * (1 - _ss((y0 - 2.45) / 0.35))
            add(np.stack([x, np.full_like(x, y0), z], 1), np.stack([s, np.zeros_like(s) - 0.2, -c], 1),
                PART_BODY, K_SURF, fade)

        # sparks around the crown and shoulders
        nd = 320
        a = rng.uniform(0, 2 * math.pi, nd)
        el = rng.uniform(-1.0, 0.2, nd)
        rr = rng.uniform(1.1, 1.45, nd)
        p = np.stack([rr * np.cos(a) * np.cos(el), -1.3 * np.abs(np.sin(el)) * rr * 0.9 - 0.1, rr * np.sin(a) * np.cos(el)], 1)
        add(p, p, PART_HEAD, K_DUST, rng.uniform(0.2, 0.6, nd))

        self.P = np.concatenate(P)
        self.N = np.concatenate(N)
        self.part = np.concatenate(part)
        self.kind = np.concatenate(kind)
        self.base = np.concatenate(base)
        self.ring = np.concatenate(ring)
        n = self.P.shape[0]
        self.n = n
        self.phase = rng.uniform(0, 2 * math.pi, n).astype(np.float32)
        self.freq = rng.uniform(0.5, 2.0, n).astype(np.float32)


        # rig weights, from rest positions
        x, y, z = self.P[:, 0], self.P[:, 1], self.P[:, 2]
        head = self.part == PART_HEAD
        u = np.clip(x / np.maximum(np.abs(_head_surface(np.full_like(y, math.pi / 2), y)[0]), 0.05), -1, 1)
        frontal = (z < 0) & head
        self.jaw_w = (_ss((y - 0.59) / 0.06) * (1 - _ss((np.abs(u) - 0.42) / 0.3)) * frontal * (y < 1.4)).astype(np.float32)
        self.lip_w = (_g(y, 0.52, 0.05) * _g(u, 0, 0.25) * frontal).astype(np.float32)
        self.mouth_w = (_g(y, 0.60, 0.13) * _g(u, 0, 0.35) * frontal).astype(np.float32)
        self.core_w = (_g(u, 0, 0.5) * _g(y, 0.18, 0.55) * frontal).astype(np.float32)
        self.yv = y.astype(np.float32)
        self.is_mouth = self.kind == K_MOUTH


class _Renderer:
    def __init__(self, n: int, scale: float) -> None:
        self.S = scale
        self.cw, self.ch = int(round(WIN_W * scale)), int(round(WIN_H * scale))
        self.k = _kernel(5, 0.75 * scale)
        self.canvas = np.zeros((self.ch, self.cw, 3), dtype=np.float32)
        self._out = np.empty((self.ch, self.cw, 4), dtype=np.uint8)

    def render(self, x, y, inten, col, trail: float) -> QImage:
        c = self.canvas
        c *= trail
        S, cw, ch = self.S, self.cw, self.ch
        kdx, kdy, kw = self.k
        xi = np.round(x * S).astype(np.int32)
        yi = np.round(y * S).astype(np.int32)
        keep = (inten > 0.004) & (xi >= 3) & (xi < cw - 3) & (yi >= 3) & (yi < ch - 3)
        if keep.any():
            flat = ((yi[keep][:, None] + kdy) * cw + (xi[keep][:, None] + kdx)).ravel()
            w = inten[keep][:, None] * kw
            cc = col[keep]
            for k in range(3):
                c[..., k] += np.bincount(flat, weights=(w * cc[:, k:k + 1]).ravel(),
                                         minlength=cw * ch).reshape(ch, cw)
        v = c * 2.0
        v = v / (1.0 + v)
        np.multiply(v, 255, out=v)
        out = self._out
        out[..., :3] = v
        out[..., 3] = out[..., :3].max(axis=2)
        img = QImage(out.data, cw, ch, 4 * cw, QImage.Format.Format_RGBA8888_Premultiplied).copy()
        img.setDevicePixelRatio(S)
        return img

    def clear(self) -> None:
        self.canvas[:] = 0


def _rot(yaw: float, pitch: float, roll: float) -> np.ndarray:
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cr, sr = math.cos(roll), math.sin(roll)
    ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]], np.float32)
    rx = np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]], np.float32)
    rz = np.array([[cr, -sr, 0], [sr, cr, 0], [0, 0, 1]], np.float32)
    return ry @ rx @ rz


class HeadPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(WIN_W, WIN_H)

        dpr = QApplication.primaryScreen().devicePixelRatio()
        self._m = _Model()
        self._r = _Renderer(self._m.n, min(dpr, 1.5))
        self._rx = lipsync_bridge.Receiver()
        self._queue: deque = deque()

        self._status = "booting"        # booting | listening | thinking | speaking | sleeping
        self._watching = False
        self._last_audio = 0.0
        self._level = self._sib = 0.0
        self._open = self._wide = self._energy = self._glow = 0.0
        self._think = 0.0
        self._t = 0.0
        self._settle = 0.0     # 0 = fully awake, 1 = settled for sleep -- slows the head's motion as it calms down
        self._blink = 0.0
        self._blink_start = None
        self._next_blink = time.monotonic() + random.uniform(1.5, 4.0)

        self._mode = "orb"              # orb | assemble | shown | dissolve
        self._mode_t0 = time.monotonic()
        self._fx: QImage | None = None
        self._progress = 0.0
        self._orb = 1.0
        self._order = None

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._step)
        self._timer.start(_TICK_MS)
        self.place()
        self.show()

    # ------------------------------------------------------------------ control

    def place(self) -> None:
        g = QApplication.primaryScreen().availableGeometry()
        self.move(g.x() + g.width() - WIN_W - MARGIN_R, g.y() + g.height() - WIN_H - MARGIN_B)

    def top_left(self) -> tuple[int, int]:
        return self.x(), self.y()

    def set_status(self, status: str) -> None:
        if status == self._status:
            return
        was = self._status
        self._status = status
        if status != "sleeping" and self._timer.interval() != _TICK_MS:
            self._timer.setInterval(_TICK_MS)       # wake: full frame rate right away
        if status == "sleeping" and self._mode in ("shown", "assemble"):
            self._begin("dissolve")
        elif status != "sleeping" and was == "sleeping" and self._mode in ("orb", "dissolve"):
            self._begin("assemble")

    def set_watching(self, watching: bool) -> None:
        self._watching = watching

    def play_assembly(self) -> None:
        self._begin("assemble")

    def _begin(self, mode: str) -> None:
        self._mode, self._mode_t0 = mode, time.monotonic()
        if mode == "assemble":
            self._r.clear()

    # ------------------------------------------------------------------ audio

    def _pull_audio(self, now: float) -> None:
        for packet in self._rx.drain():
            self._queue.append(packet)
        applied = None
        while self._queue and self._queue[0][2] + _AUDIO_LATENCY_S <= now:
            applied = self._queue.popleft()
        if len(self._queue) > 64:
            applied = self._queue[-1]
            self._queue.clear()
        if applied is not None:
            self._level, self._sib = applied[0], applied[1]
            if self._level > 0.03:
                self._last_audio = now
        elif now - self._last_audio > 0.25:
            self._level *= 0.6

    # ------------------------------------------------------------------ tick

    def _step(self) -> None:
        now = time.monotonic()
        self._pull_audio(now)
        # Settling is slow going to sleep (a gradual calm-down) and quick coming
        # out of it (waking up reads better snappy) -- and it scales _t itself,
        # so every sine-driven motion (breathing, head sway, scan wave, sparks)
        # visibly slows down as he settles instead of freezing and cutting out.
        ticks = self._timer.interval() / _TICK_MS   # >1 at the idle frame rate
        settle_target = 1.0 if self._status == "sleeping" else 0.0
        rate = 0.016 if settle_target > self._settle else 0.06
        self._settle += (settle_target - self._settle) * (1 - (1 - rate) ** ticks)
        self._t += (_TICK_MS / 1000) * ticks * (1.0 - 0.6 * self._settle)
        talking = self._status == "speaking" or (now - self._last_audio) < 0.4
        lvl = self._level if talking else 0.0
        self._open += (lvl - self._open) * (0.55 if lvl > self._open else 0.3)
        self._wide += (self._sib * min(1.0, lvl * 3) - self._wide) * 0.35
        self._energy += (lvl - self._energy) * (0.45 if lvl > self._energy else 0.15)
        self._glow += (min(1.0, lvl * 1.4) - self._glow) * 0.05
        self._think += ((1.0 if self._status == "thinking" else 0.0) - self._think) * 0.06
        self._talking = talking
        self._step_blink(now)
        self._step_fx(now, ticks)
        self.update()
        # Asleep and fully settled into the orb: nothing moves fast, so drop to
        # the idle frame rate instead of redrawing 60 times a second.
        idle = self._status == "sleeping" and self._mode == "orb" and self._settle > 0.97
        want = _IDLE_TICK_MS if idle else _TICK_MS
        if self._timer.interval() != want:
            self._timer.setInterval(want)

    def _step_blink(self, now: float) -> None:
        if self._blink_start is None and now >= self._next_blink:
            self._blink_start = now
        if self._blink_start is not None:
            p = (now - self._blink_start) / 0.16
            self._blink = math.sin(min(1.0, p) * math.pi)
            if p >= 1.0:
                self._blink_start, self._blink = None, 0.0
                base = 0.2 if random.random() < 0.15 else random.uniform(2.5, 5.5)
                self._next_blink = now + base * (1.0 + 1.5 * self._settle)

    def _pose(self):
        """Deform + rotate + light + project the model for this frame."""
        m, t, e = self._m, self._t, self._energy
        P = m.P.copy()
        J = float(np.clip((self._open - 0.05) / 0.6, 0, 1))
        W = float(np.clip(self._wide * 1.4, 0, 1))

        # jaw, lips, mouth width
        P[:, 1] += m.jaw_w * J * 0.24 * (1 - 0.45 * W)
        P[:, 2] += m.jaw_w * J * 0.05
        P[:, 1] -= m.lip_w * J * 0.035
        P[:, 0] *= 1 + m.mouth_w * W * 0.14
        mo = m.is_mouth
        P[mo, 1] = 0.60 + m.mouth_t * J * 0.24 * (1 - 0.45 * W) * 0.95
        # blink: squash the eye clusters vertically
        eye = m.kind == K_EYE
        P[eye, 1] = -0.12 + (P[eye, 1] + 0.12) * (1 - 0.92 * self._blink)
        # sparks drift up
        dust = m.kind == K_DUST
        drift = (t * 0.12 * m.freq[dust] + m.phase[dust]) % 0.6
        P[dust, 1] -= drift

        # head motion: slow looks around, a tilt, nods with the voice
        yaw = 0.26 * math.sin(t * 0.33) + 0.08 * math.sin(t * 0.87 + 1.3)
        pitch = 0.05 * math.sin(t * 0.52) + 0.05 * e * math.sin(t * 7.0) - 0.04 * self._think
        roll = 0.04 * math.sin(t * 0.41 + 0.7) + 0.05 * self._think
        head = m.part == PART_HEAD
        neck = m.part == PART_NECK
        body = m.part == PART_BODY
        N = m.N.copy()
        for mask, k in ((head, 1.0), (neck, 0.5), (body, 0.22)):
            R = _rot(yaw * k, pitch * k, roll * k)
            P[mask] = (P[mask] - _PIVOT) @ R.T + _PIVOT
            N[mask] = N[mask] @ R.T
        breathe = 0.012 * math.sin(t * 1.5)
        P[:, 1] -= breathe * (P[:, 1] < 1.65)

        # projection
        zc = P[:, 2] + _CAM_D
        k = _CAM_D / zc
        sx = WIN_W / 2 + _SCALE * P[:, 0] * k
        sy = _TOP + _SCALE * (P[:, 1] + 1.3) * k

        # lighting: fresnel rim + key light; far side faint
        nz = N[:, 2]
        facing = -nz
        fres = (1 - np.abs(nz)) ** 2.2
        light = np.clip(N @ _LIGHT, 0, 1)
        inten = m.base * (0.10 + 0.45 * light * np.clip(facing, 0, 1) + 1.05 * fres)
        inten = np.where(facing < -0.05, inten * 0.22, inten)

        col = np.empty((m.n, 3), np.float32)
        col[:] = CYAN
        col += (RIM - CYAN) * np.clip(fres * 1.3, 0, 1)[:, None]

        # the face core: orange behind the face, burning with the voice / cyan while thinking
        cw = m.core_w
        heat = np.clip(cw * (0.25 + 1.6 * e), 0, 1)
        core_col = ORANGE + (YELLOW - ORANGE) * heat[:, None]
        core_col = core_col + (WHITE - core_col) * self._think
        mix = np.clip(cw * 1.3, 0, 1)[:, None]
        col = col + (core_col - col) * mix
        inten = inten * (1 + cw * (0.35 + 1.4 * e + 0.4 * self._think))

        # scan wave through the rings
        speed = 1.2 + 6.0 * self._think
        wave = 0.5 + 0.5 * np.sin(m.yv * (5 + 4 * self._think) - t * speed * 2)
        inten = inten * (0.8 + (0.25 + 0.5 * self._think) * wave ** 3)

        # eyes, mouth light, nerves, sparks
        inten[eye] = 1.3 * (0.65 + 0.35 * e) * (1 - 0.7 * self._blink)
        col[eye] = WHITE
        inten[mo] = m.base[mo] * J * (0.9 + 0.8 * e)
        col[mo] = YELLOW
        nerve = m.kind == K_NERVE
        inten[nerve] = 0.55 * (0.7 + 0.3 * np.sin(t * 4 - m.yv[nerve] * 6)) + 0.5 * e
        col[nerve] = GOLD
        inten[dust] = m.base[dust] * (0.5 + 0.5 * np.sin(t * 3 * m.freq[dust] + m.phase[dust])) \
            * (1 - drift / 0.6) * (1 + e)
        return sx.astype(np.float32), sy.astype(np.float32), inten.astype(np.float32), col

    def _flight(self, hx, hy, p):
        """Orb -> home along a wide outward curve (quadratic Bezier), p in 0..1 per point."""
        m = self._m
        cx, cy = WIN_W / 2, WIN_H * 0.45
        ox, oy = hx - cx, hy - cy
        norm = np.maximum(np.sqrt(ox * ox + oy * oy), 1e-3)
        ctrl_x = (ORB[0] + hx) / 2 + ox / norm * 95 + np.sin(m.phase) * 10
        ctrl_y = (ORB[1] + hy) / 2 + oy / norm * 95 - 45 + np.cos(m.phase) * 10
        a, b, c = (1 - p) ** 2, 2 * (1 - p) * p, p * p
        return a * ORB[0] + b * ctrl_x + c * hx, a * ORB[1] + b * ctrl_y + c * hy

    def _order_for(self, hx, hy):
        if self._order is None:
            ang = np.degrees(np.arctan2(hy - WIN_H * 0.5, hx - WIN_W / 2))
            s = np.mod(ang - 143.0, 360.0)
            self._order = np.clip(s / 256.0, 0, 1).astype(np.float32)
        return self._order

    def _step_fx(self, now: float, ticks: float = 1.0) -> None:
        t = now - self._mode_t0
        if self._mode == "orb":
            target = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(self._t * (1.6 if self._status == "sleeping" else 3.0)))
            # Eased rather than assigned outright: whatever _orb was doing in
            # the mode we just left (mid-flash from "assemble", mid-fade from
            # "dissolve"), it glides onto this pulse instead of popping onto it.
            self._orb += (target - self._orb) * (1 - 0.95 ** ticks)
            self._fx = None
            self._progress = 0.0
            return
        hx, hy, hi, hc = self._pose()
        order = self._order_for(hx, hy)
        if self._mode == "assemble":
            lead, spread, fly = 0.45, 1.9, 0.8
            emit = lead + spread * order + 0.1 * (self._m.phase / (2 * math.pi))
            p = np.clip((t - emit) / fly, 0, 1)
            x, y = self._flight(hx, hy, _ss(p))
            landed = np.clip((t - emit - fly) / 0.35, 0, 1)
            flying = (p > 0) & (p < 1)
            inten = np.where(p <= 0, 0.0, np.where(flying, 0.7, hi * landed + 0.75 * (1 - landed)))
            col = hc + (RIM - hc) * (1 - landed)[:, None]
            self._progress = float((p >= 1).mean())
            target_orb = 1.0 - _ss((t - (lead + spread + fly)) / 0.5)
            self._orb += (target_orb - self._orb) * 0.25
            self._fx = self._r.render(x, y, inten.astype(np.float32), col, trail=0.6)
            if t >= lead + spread + fly + 0.5:
                self._mode, self._progress = "shown", 1.0
        elif self._mode == "dissolve":
            # Noticeably slower and gentler than the assemble above: the head
            # streams back into the orb over a longer, easier curve (instead
            # of a quick snap-out), matching _settle's slow-down of its motion.
            spread, fly = 1.3, 1.0
            start = spread * (1 - order)
            p = _ss(np.clip((t - start) / fly, 0, 1))
            x, y = self._flight(hx, hy, 1 - p)
            inten = np.where(p >= 1, 0.0, hi * (1 - p) + 0.7 * np.sin(p * math.pi))
            col = hc + (RIM - hc) * p[:, None]
            target_orb = min(1.0, t / 0.6)
            self._orb += (target_orb - self._orb) * 0.10
            self._progress = float((p < 1).mean())
            self._fx = self._r.render(x, y, inten.astype(np.float32), col, trail=0.72)
            if t >= spread + fly + 0.5:
                self._mode, self._fx = "orb", None
                self._r.clear()
        else:
            self._orb += (0.0 - self._orb) * 0.3
            self._fx = self._r.render(hx, hy, hi, hc, trail=0.28)

    # ------------------------------------------------------------------ paint

    def paintEvent(self, _) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        shown = 1.0 if self._mode == "shown" else self._progress

        # dark glass behind the head so the glow reads over any window
        a = max(shown, 0.35 * self._orb)
        back = QRadialGradient(QPointF(WIN_W / 2, WIN_H * 0.5), WIN_W * 0.62)
        back.setColorAt(0.0, QColor(6, 10, 18, int(220 * a)))
        back.setColorAt(0.7, QColor(6, 10, 18, int(150 * a)))
        back.setColorAt(1.0, QColor(6, 10, 18, 0))
        p.setBrush(back)
        p.drawEllipse(QPointF(WIN_W / 2, WIN_H * 0.5), WIN_W * 0.62, WIN_H * 0.56)

        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        if shown > 0.02:
            e = self._energy
            cx, cy = WIN_W / 2, _TOP + _SCALE * (1.3 + 0.15)
            r = 44 + 16 * e
            core = QRadialGradient(QPointF(cx, cy), r)
            if self._think > 0.5:
                core.setColorAt(0.0, QColor(200, 240, 255, int(shown * 90)))
                core.setColorAt(1.0, QColor(80, 180, 255, 0))
            else:
                core.setColorAt(0.0, QColor(255, 230, 160, int(shown * min(255, 40 + 190 * e))))
                core.setColorAt(0.45, QColor(255, 120, 30, int(shown * min(255, 45 + 110 * e))))
                core.setColorAt(1.0, QColor(255, 100, 10, 0))
            p.setBrush(core)
            p.drawEllipse(QPointF(cx, cy), r, r * 1.25)

        if self._fx is not None:
            p.drawImage(QRectF(0, 0, WIN_W, WIN_H), self._fx)

        if self._orb > 0.01:
            pulse = 1.0 + 0.12 * math.sin(self._t * 6)
            r = (13 if self._mode != "orb" else 10) * pulse
            g = QRadialGradient(QPointF(ORB[0], ORB[1]), r * 2.6)
            g.setColorAt(0.0, QColor(255, 255, 255, int(255 * self._orb)))
            g.setColorAt(0.18, QColor(120, 210, 255, int(235 * self._orb)))
            g.setColorAt(1.0, QColor(40, 140, 255, 0))
            p.setBrush(g)
            p.drawEllipse(QPointF(ORB[0], ORB[1]), r * 2.6, r * 2.6)

        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        self._paint_label(p)

    def _paint_label(self, p: QPainter) -> None:
        if self._mode == "assemble":
            text = f"ASSEMBLING... {int(self._progress * 100)}%"
        elif self._mode == "dissolve" or self._status == "sleeping":
            text = "STANDBY  ·  F10"
        elif self._status == "booting":
            text = "BOOTING..."
        else:
            text = {"thinking": "STATUS: THINKING", "speaking": "STATUS: SPEAKING"}.get(
                "speaking" if self._talking else self._status, "STATUS: LISTENING")
        font = QFont("Consolas", 7)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
        p.setFont(font)
        p.setPen(QColor(110, 205, 255, 200))
        p.drawText(QRectF(0, 4, WIN_W - 10, 14), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, text)
        if self._watching:
            # screen capture is running (tools/screen_watch.py): always visible while it lasts
            pulse = 0.5 + 0.5 * abs(math.sin(self._t * 2.2))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 120, 110, int(140 + 110 * pulse)))
            p.drawEllipse(QPointF(12, 11), 3.5 + 1.5 * pulse, 3.5 + 1.5 * pulse)


def demo() -> None:
    """python hud_head3d.py -- orb, assembly, listening, thinking, talking, sleep."""
    import sys

    app = QApplication.instance() or QApplication(sys.argv)
    head = HeadPanel()
    head._pull_audio = lambda now: None
    start = time.monotonic()
    QTimer.singleShot(800, head.play_assembly)

    def script():
        t = time.monotonic() - start
        status = ("booting" if t < 0.8 else "listening" if t < 5 else "thinking" if t < 7
                  else "speaking" if t < 14 else "listening" if t < 16 else "sleeping")
        head.set_status(status)
        if status == "speaking":
            syl = max(0.0, math.sin(t * 2 * math.pi * 3.6)) ** 0.6
            phrase = 1.0 if (t % 3.5) < 2.7 else 0.0
            head._level = 0.85 * syl * phrase
            head._sib = 1.0 if int(t * 3.6) % 5 == 2 else 0.1
            head._last_audio = time.monotonic()
        else:
            head._level = 0.0

    feeder = QTimer()
    feeder.timeout.connect(script)
    feeder.start(16)
    QTimer.singleShot(19000, app.quit)
    app.exec()


if __name__ == "__main__":
    demo()
