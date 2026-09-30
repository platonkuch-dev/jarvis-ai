"""HumanoidPanel: a faceless holographic humanoid (head, neck, shoulders)
that streams out of a single orb and glows while Jarvis talks.

The look: the figure is drawn by thousands of points laid along
  * horizontal contour lines across the head and neck, curved like latitude
    lines on a surface, brighter towards the silhouette (rim light);
  * concentric arcs across the shoulders;
  * a dense, bright outline;
  * an orange core where the face would be, made of the same lines but wavy;
  * gold "nerve" lines running up the neck;
  * loose sparks drifting off the top of the head and the edges.

Talking: there is no mouth. The live audio level (lipsync_bridge) makes the
face core swell and burn from orange to yellow-white, its lines ripple
faster and harder (sibilants ripple finer), and the outline flares.

Entrances/exits: a blue orb appears at the base of the neck and shoots a
stream of particles that sweeps round the figure -- left shoulder, up over
the head, down the right side -- each particle curving out wide before it
lands in its place ("ASSEMBLING... NN%"). Every later reply replays it
quickly; when he stops talking the particles stream back into the orb.

No image assets: everything is generated here and rendered additively with
numpy (the same splat renderer idea as hud_avatar.py).
"""

from __future__ import annotations

import math
import time
from collections import deque

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QRadialGradient
from PyQt6.QtWidgets import QApplication, QWidget

import lipsync_bridge

WIN_W, WIN_H = 460, 440
CX = WIN_W / 2
ORB = np.array([CX, 410.0], dtype=np.float32)
FACE_C = np.array([CX, 138.0], dtype=np.float32)      # centre of the orange core
_GAP = 4
_TICK_MS = 16
_AUDIO_LATENCY_S = 0.07
_HOLD_AFTER_SPEECH_S = 1.1
_WAIT_FOR_GREETING_S = 7.0

CYAN = np.array([0.10, 0.52, 1.00], dtype=np.float32)
RIM = np.array([0.50, 0.88, 1.00], dtype=np.float32)
ORANGE = np.array([1.00, 0.42, 0.06], dtype=np.float32)
YELLOW = np.array([1.00, 0.93, 0.62], dtype=np.float32)
GOLD = np.array([1.00, 0.66, 0.22], dtype=np.float32)

KIND_CONTOUR, KIND_FACE, KIND_RIM, KIND_GLOW, KIND_NECK, KIND_DUST = range(6)

# (y, half-width) along the silhouette, top of the head to the bottom edge
_PROFILE = [(26, 0), (30, 22), (38, 38), (52, 52), (72, 61), (98, 65), (124, 64), (148, 60),
            (166, 54), (182, 46), (196, 39), (208, 36), (222, 36), (236, 38), (250, 47),
            (264, 66), (280, 98), (298, 133), (318, 165), (342, 189), (372, 204), (440, 212)]


def _smoothstep(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


def _kernel(size: int, sigma: float):
    r = np.arange(size) - size // 2
    kx, ky = np.meshgrid(r, r)
    return kx.ravel(), ky.ravel(), np.exp(-(kx * kx + ky * ky).ravel() / (2 * sigma * sigma)).astype(np.float32)


class _Figure:
    """The point cloud: home positions, kinds, base colours/intensities."""

    def __init__(self, seed: int = 11) -> None:
        rng = np.random.default_rng(seed)
        ys = np.arange(20, WIN_H + 1, 1.0)
        py, pw = zip(*_PROFILE)
        hw = np.interp(ys, py, pw)
        k = np.ones(9) / 9
        hw = np.convolve(np.pad(hw, 4, mode="edge"), k, mode="valid")
        self._ys, self._hw = ys, hw

        xs_l, ys_l, kind, inten, line_id = [], [], [], [], []

        def add(x, y, kd, it, lid=0):
            xs_l.append(np.asarray(x, dtype=np.float32))
            ys_l.append(np.asarray(y, dtype=np.float32))
            n = np.size(x)
            kind.append(np.full(n, kd, dtype=np.int8))
            inten.append(np.broadcast_to(np.asarray(it, dtype=np.float32), (n,)).copy())
            line_id.append(np.full(n, lid, dtype=np.int16))

        # head + neck: curved horizontal contour lines
        for i, y0 in enumerate(np.arange(34, 250, 4.3)):
            w = self.halfw(y0) * 0.965
            if w < 5:
                continue
            x = np.arange(-w, w + 0.1, 2.0)
            u = x / w
            y = y0 + 2.6 * (1 - u * u)
            edge = np.abs(u) ** 3
            face = ((x / 45.0) ** 2 + ((y - FACE_C[1]) / 52.0) ** 2) < 1.0
            if face.any():
                add(CX + x[face], y[face], KIND_FACE, 0.95, i)
            add(CX + x[~face], y[~face], KIND_CONTOUR, 0.07 + 0.85 * edge[~face], i)

        # shoulders: concentric arcs around a point below the frame
        acx, acy = CX, WIN_H + 60
        for r in np.arange(80, 330, 9.0):
            ang = np.linspace(-math.pi, 0, int(math.pi * r / 2.2))
            x = acx + r * np.cos(ang)
            y = acy + r * np.sin(ang)
            inside = (y > 244) & (y < WIN_H - 4) & (np.abs(x - CX) < self.halfw(y) * 0.975) & (np.abs(x - CX) > 40)
            if inside.any():
                u = np.abs(x[inside] - CX) / self.halfw(y[inside])
                fade = 1.0 - _smoothstep((y[inside] - 380) / 55)
                add(x[inside], y[inside], KIND_CONTOUR, (0.06 + 0.8 * u ** 4) * fade)

        # silhouette: dense bright rim, plus a soft wide glow copy
        yy = np.arange(27, WIN_H - 2, 0.8)
        w = self.halfw(yy)
        fade = 1.0 - _smoothstep((yy - 395) / 45)
        for side in (-1, 1):
            add(CX + side * w, yy, KIND_RIM, 1.0 * fade)
        yy2 = np.arange(27, WIN_H - 2, 3.0)
        w2 = self.halfw(yy2)
        f2 = 1.0 - _smoothstep((yy2 - 395) / 45)
        for side in (-1, 1):
            add(CX + side * w2, yy2, KIND_GLOW, 0.5 * f2)
        # crown: close the top of the head
        a = np.linspace(-math.pi, 0, 60)
        add(CX + 22 * np.cos(a), 30 + 5 * np.sin(a), KIND_RIM, 0.9)

        # gold nerve lines up the neck, with a few branches
        for j in range(5):
            x, y = CX + rng.uniform(-30, 30), rng.uniform(270, 300)
            pts_x, pts_y = [], []
            while y > 200:
                pts_x.append(x)
                pts_y.append(y)
                y -= 1.6
                x += rng.normal(0, 1.4) + (CX - x) * 0.025
                if rng.random() < 0.05:            # a small side branch
                    bx, by = x, y
                    for _ in range(rng.integers(8, 20)):
                        bx += rng.choice([-1, 1]) * 1.4
                        by -= 1.0
                        pts_x.append(bx)
                        pts_y.append(by)
            add(pts_x, pts_y, KIND_NECK, 0.55)

        # loose sparks: off the crown and the edges
        n_dust = 700
        top = rng.random(n_dust) < 0.55
        yy3 = np.where(top, rng.uniform(26, 120, n_dust), rng.uniform(60, 420, n_dust))
        side = rng.choice([-1, 1], n_dust)
        out = rng.exponential(10, n_dust)
        dx = side * (self.halfw(yy3) + out)
        dy = np.where(top, -rng.exponential(12, n_dust) * (1 - np.abs(dx) / 80).clip(0, 1), 0)
        add(CX + dx, yy3 + dy, KIND_DUST, rng.uniform(0.15, 0.5, n_dust))

        self.x = np.concatenate(xs_l)
        self.y = np.concatenate(ys_l)
        self.kind = np.concatenate(kind)
        self.inten = np.concatenate(inten)
        self.line = np.concatenate(line_id)
        n = self.x.size
        self.n = n
        self.phase = rng.uniform(0, 2 * math.pi, n).astype(np.float32)
        self.freq = rng.uniform(0.5, 2.0, n).astype(np.float32)

        col = np.empty((n, 3), dtype=np.float32)
        col[:] = CYAN
        col[self.kind == KIND_RIM] = RIM
        col[self.kind == KIND_GLOW] = CYAN
        col[self.kind == KIND_NECK] = GOLD
        col[self.kind == KIND_FACE] = ORANGE
        self.col = col

        fdx = (self.x - FACE_C[0]) / 45.0
        fdy = (self.y - FACE_C[1]) / 52.0
        self.face_r = np.sqrt(fdx * fdx + fdy * fdy).astype(np.float32)

        # sweep order for the stream: left shoulder -> over the head -> right side
        ang = np.degrees(np.arctan2(self.y - 235.0, self.x - CX))
        s = np.mod(ang - 143.0, 360.0)
        self.order = np.clip(s / 256.0, 0, 1).astype(np.float32)
        # interior points land a touch after the outline around them
        self.order = np.clip(self.order + 0.06 * (self.kind == KIND_FACE) + 0.04 * (self.kind == KIND_CONTOUR), 0, 1)
        outward = np.stack([self.x - CX, self.y - 235.0], axis=1)
        outward /= np.maximum(np.linalg.norm(outward, axis=1, keepdims=True), 1e-3)
        self.outward = outward.astype(np.float32)
        self.jitter = rng.normal(0, 1, (n, 2)).astype(np.float32)

    def halfw(self, y):
        return np.interp(y, self._ys, self._hw)


class _Renderer:
    def __init__(self, fig: _Figure, scale: float) -> None:
        self.S = scale
        self.cw, self.ch = int(round(WIN_W * scale)), int(round(WIN_H * scale))
        self.big = (fig.kind == KIND_GLOW)
        self.k_fine = _kernel(5, 0.75 * scale)
        self.k_big = _kernel(int(2 * round(4 * scale) + 1), 2.8 * scale)
        self.canvas = np.zeros((self.ch, self.cw, 3), dtype=np.float32)
        self._out = np.empty((self.ch, self.cw, 4), dtype=np.uint8)

    def render(self, x, y, inten, col, trail: float) -> QImage:
        c = self.canvas
        c *= trail
        S, cw, ch = self.S, self.cw, self.ch
        xi = np.round(x * S).astype(np.int32)
        yi = np.round(y * S).astype(np.int32)
        for mask, (kdx, kdy, kw) in ((~self.big, self.k_fine), (self.big, self.k_big)):
            pad = int(np.abs(kdx).max()) + 1
            keep = mask & (inten > 0.004) & (xi >= pad) & (xi < cw - pad) & (yi >= pad) & (yi < ch - pad)
            if not keep.any():
                continue
            flat = ((yi[keep][:, None] + kdy) * cw + (xi[keep][:, None] + kdx)).ravel()
            w = inten[keep][:, None] * kw
            cc = col[keep]
            for k in range(3):
                c[..., k] += np.bincount(flat, weights=(w * cc[:, k:k + 1]).ravel(),
                                         minlength=cw * ch).reshape(ch, cw)
        v = c * 2.1
        v = v / (1.0 + v)                                    # soft tone-map: bloom instead of clipping
        out = self._out
        np.multiply(v, 255, out=v)
        out[..., :3] = v                                    # premultiplied colour
        out[..., 3] = out[..., :3].max(axis=2)
        img = QImage(out.data, cw, ch, 4 * cw, QImage.Format.Format_RGBA8888_Premultiplied).copy()
        img.setDevicePixelRatio(S)
        return img

    def clear(self) -> None:
        self.canvas[:] = 0


class HumanoidPanel(QWidget):
    def __init__(self, anchor, parent=None):
        """anchor() -> (x_center, y_top) in screen coordinates: where the figure hangs from."""
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
        self._anchor = anchor

        dpr = QApplication.primaryScreen().devicePixelRatio()
        self._fig = _Figure()
        self._r = _Renderer(self._fig, min(dpr, 1.25))    # fine enough for glowing points, half the cost of 1.5x

        self._rx = lipsync_bridge.Receiver()
        self._queue: deque = deque()
        self._speaking = False
        self._last_audio = 0.0
        self._level = self._sib = 0.0
        self._energy = 0.0           # fast-ish follower of the voice
        self._glow = 0.0             # slow follower: overall "burn"
        self._t = 0.0

        self._mode = "hidden"
        self._mode_t0 = 0.0
        self._linger_until = 0.0
        self._fx: QImage | None = None
        self._progress = 0.0         # share of the figure that has landed (for the label)
        self._orb = 0.0              # orb brightness 0..1

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._step)
        self._timer.start(_TICK_MS)

    # ------------------------------------------------------------------ control

    def set_speaking(self, speaking: bool) -> None:
        self._speaking = speaking

    def play_assembly(self) -> None:
        self._start("assemble")
        self._linger_until = time.monotonic() + _WAIT_FOR_GREETING_S

    def _start(self, mode: str) -> None:
        self._mode, self._mode_t0 = mode, time.monotonic()
        if mode in ("assemble", "materialize"):
            self._r.clear()
        if not self.isVisible():
            self._place()
            self.show()

    def _place(self) -> None:
        x, y = self._anchor()
        self.move(int(x - WIN_W / 2), int(y + _GAP))

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
        talking = self._speaking or (now - self._last_audio) < _HOLD_AFTER_SPEECH_S
        wanted = talking or now < self._linger_until
        if talking:
            self._linger_until = 0.0
        if wanted and self._mode in ("hidden", "dissolve"):
            self._start("materialize")
        elif not wanted and self._mode == "shown":
            self._start("dissolve")
        if self._mode == "hidden":
            return

        self._place()
        self._t += _TICK_MS / 1000
        lvl = self._level if talking else 0.0
        self._energy += (lvl - self._energy) * (0.45 if lvl > self._energy else 0.18)
        self._glow += (min(1.0, lvl * 1.4) - self._glow) * 0.05
        self._talking = talking
        self._step_fx(now)
        self.update()

    def _live(self):
        """Home positions/colours/intensities of the finished figure, animated."""
        f, t, e, g = self._fig, self._t, self._energy, self._glow
        x = f.x.copy()
        y = f.y.copy()
        inten = f.inten.copy()
        col = f.col.copy()

        # breathing
        s = 1.0 + 0.004 * math.sin(t * 1.5)
        x = CX + (x - CX) * s
        y = 235 + (y - 235) * s + 0.8 * math.sin(t * 1.5)

        # face core: rippling lines, swelling and burning with the voice
        face = f.kind == KIND_FACE
        amp = 0.7 + 4.2 * e
        kx = 0.13 + 0.10 * self._sib
        y[face] += amp * np.sin(kx * (f.x[face] - CX) + t * (2.2 + 9.0 * e) + f.line[face] * 0.55) \
            * (1.0 - 0.5 * f.face_r[face])
        heat = np.clip((1.0 - f.face_r[face]) ** 0.8 * (0.2 + 1.8 * e), 0, 1)[:, None]
        col[face] = ORANGE + (YELLOW - ORANGE) * heat
        inten[face] *= (0.75 + 1.9 * e + 0.3 * g) * (0.5 + 0.5 * (1 - f.face_r[face]))

        # rim flares with the voice, contour lines shimmer
        rim = (f.kind == KIND_RIM) | (f.kind == KIND_GLOW)
        inten[rim] *= 1.0 + 0.7 * e + 0.3 * g
        cont = f.kind == KIND_CONTOUR
        inten[cont] *= 0.85 + 0.15 * np.sin(t * 1.8 + f.y[cont] * 0.09) + 0.35 * g

        # sparks drift upward and twinkle
        dust = f.kind == KIND_DUST
        drift = (t * 6 * f.freq[dust] + f.phase[dust] * 10) % 40
        y[dust] -= drift
        x[dust] += 2.5 * np.sin(t * f.freq[dust] + f.phase[dust])
        inten[dust] *= (0.5 + 0.5 * np.sin(t * 3 * f.freq[dust] + f.phase[dust])) * (1 - drift / 40) * (1 + e)

        neck = f.kind == KIND_NECK
        inten[neck] *= 0.75 + 0.25 * np.sin(t * 4 - f.y[neck] * 0.08) + 0.6 * e
        return x, y, inten, col

    def _flight(self, hx, hy, p):
        """Orb -> home along a wide outward curve (quadratic Bezier), p in 0..1 per point."""
        f = self._fig
        mx, my = (ORB[0] + hx) / 2, (ORB[1] + hy) / 2
        ctrl_x = mx + f.outward[:, 0] * 150 + f.jitter[:, 0] * 18
        ctrl_y = my + f.outward[:, 1] * 150 - 70 + f.jitter[:, 1] * 18
        a, b, c = (1 - p) ** 2, 2 * (1 - p) * p, p * p
        return a * ORB[0] + b * ctrl_x + c * hx, a * ORB[1] + b * ctrl_y + c * hy

    def _step_fx(self, now: float) -> None:
        t = now - self._mode_t0
        f = self._fig
        hx, hy, hi, hc = self._live()
        if self._mode in ("assemble", "materialize"):
            full = self._mode == "assemble"
            lead, spread, fly = (0.55, 2.3, 0.85) if full else (0.12, 0.75, 0.45)
            emit = lead + spread * f.order + (0.25 if full else 0.08) * np.abs(f.jitter[:, 0]) * 0.4
            p = np.clip((t - emit) / fly, 0, 1)
            pe = _smoothstep(p)
            x, y = self._flight(hx, hy, pe)
            landed = np.clip((t - emit - fly) / 0.35, 0, 1)
            flying = (p > 0) & (p < 1)
            inten = np.where(p <= 0, 0.0, np.where(flying, 0.75, hi * landed + 0.8 * (1 - landed)))
            col = hc.copy()
            blend = (1 - landed)[:, None]
            col = col + (RIM - col) * blend
            self._progress = float((p >= 1).mean())
            self._orb = float(min(1.0, t / 0.35)) * (1.0 - _smoothstep((t - (lead + spread + fly)) / 0.6))
            self._fx = self._r.render(x, y, inten.astype(np.float32), col, trail=0.6)
            if t >= lead + spread + fly + 0.6:
                self._mode, self._progress = "shown", 1.0
        elif self._mode == "dissolve":
            spread, fly = 0.6, 0.45
            start = spread * (1 - f.order)
            p = _smoothstep(np.clip((t - start) / fly, 0, 1))
            x, y = self._flight(hx, hy, 1 - p)
            inten = np.where(p >= 1, 0.0, hi * (1 - p) + 0.7 * np.sin(p * math.pi))
            col = hc + (RIM - hc) * p[:, None]
            self._orb = float(min(1.0, t / 0.2)) * (1.0 - _smoothstep((t - spread - fly) / 0.35))
            self._progress = float((p < 1).mean())
            self._fx = self._r.render(x, y, inten.astype(np.float32), col, trail=0.6)
            if t >= spread + fly + 0.4:
                self._mode, self._fx, self._orb = "hidden", None, 0.0
                self._r.clear()
                self.hide()
        else:
            self._orb = 0.0
            self._fx = self._r.render(hx, hy, hi, hc, trail=0.3)

    # ------------------------------------------------------------------ paint

    def paintEvent(self, _) -> None:
        if self._mode == "hidden":
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        shown = 1.0 if self._mode == "shown" else self._progress

        # dark glass behind the figure, like the dark screen in the reference:
        # the glow needs a dark ground whatever window is underneath
        back = QRadialGradient(QPointF(CX, 215), 240)
        back.setColorAt(0.0, QColor(6, 10, 18, int(225 * max(shown, self._orb * 0.5))))
        back.setColorAt(0.65, QColor(6, 10, 18, int(170 * max(shown, self._orb * 0.5))))
        back.setColorAt(1.0, QColor(6, 10, 18, 0))
        p.setBrush(back)
        p.drawEllipse(QPointF(CX, 215), 240, 240)

        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        if shown > 0.02:
            # the burning core behind the face lines
            e, g = self._energy, self._glow
            r = 56 + 22 * e
            core = QRadialGradient(QPointF(FACE_C[0], FACE_C[1]), r)
            core.setColorAt(0.0, QColor(255, 235, 170, int(shown * min(255, 60 + 220 * e))))
            core.setColorAt(0.4, QColor(255, 130, 30, int(shown * min(255, 70 + 120 * e + 30 * g))))
            core.setColorAt(1.0, QColor(255, 100, 10, 0))
            p.setBrush(core)
            p.drawEllipse(QPointF(FACE_C[0], FACE_C[1]), r, r * 1.2)

        if self._fx is not None:
            p.drawImage(QRectF(0, 0, WIN_W, WIN_H), self._fx)

        if self._orb > 0.01:
            pulse = 1.0 + 0.12 * math.sin(self._t * 9)
            r = 16 * pulse
            g = QRadialGradient(QPointF(ORB[0], ORB[1]), r * 2.4)
            g.setColorAt(0.0, QColor(255, 255, 255, int(255 * self._orb)))
            g.setColorAt(0.18, QColor(120, 210, 255, int(240 * self._orb)))
            g.setColorAt(1.0, QColor(40, 140, 255, 0))
            p.setBrush(g)
            p.drawEllipse(QPointF(ORB[0], ORB[1]), r * 2.4, r * 2.4)

        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        self._paint_label(p)

    def _paint_label(self, p: QPainter) -> None:
        if self._mode in ("assemble", "materialize"):
            text = f"ASSEMBLING... {int(self._progress * 100)}%"
        elif self._mode == "dissolve":
            text = "STATUS: STANDBY"
        else:
            text = "STATUS: SPEAKING" if getattr(self, "_talking", False) else "STATUS: ONLINE"
        font = QFont("Consolas", 7)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
        p.setFont(font)
        p.setPen(QColor(110, 205, 255, 210))
        p.drawText(QRectF(CX + 105, 196, 150, 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
        p.setPen(QColor(110, 205, 255, 120))
        p.drawLine(QPointF(CX + 78, 203), QPointF(CX + 100, 203))


def demo() -> None:
    """python hud_humanoid.py -- assembly + fake talking, no worker needed."""
    import sys

    import ui_fonts
    from compact_bar import CompactBar, _HEIGHT

    app = QApplication.instance() or QApplication(sys.argv)
    ui_fonts.load_bundled_fonts()
    bar = CompactBar()
    bar._show_sig.emit()
    panel = HumanoidPanel(lambda: (bar.x() + bar.width() / 2, bar.y() + _HEIGHT))
    panel._pull_audio = lambda now: None
    panel.play_assembly()
    start = time.monotonic()

    def fake_voice():
        t = time.monotonic() - start
        speaking = 4.2 < t < 12
        panel.set_speaking(speaking)
        bar._state_sig.emit("SPEAKING" if speaking else "LISTENING")
        if speaking:
            syl = max(0.0, math.sin(t * 2 * math.pi * 3.6)) ** 0.6
            phrase = 1.0 if (t % 3.5) < 2.7 else 0.0
            panel._level = 0.85 * syl * phrase
            panel._sib = 1.0 if int(t * 3.6) % 5 == 2 else 0.1
            panel._last_audio = time.monotonic()

    feeder = QTimer()
    feeder.timeout.connect(fake_voice)
    feeder.start(16)
    QTimer.singleShot(17000, app.quit)
    app.exec()


if __name__ == "__main__":
    demo()
