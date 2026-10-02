"""CorePanel: Jarvis as a living holographic core in the bottom-right corner.

Why not a face: every current voice assistant that feels good (Siri's glow,
ChatGPT's voice blob, ElevenLabs' orb) is abstract -- a human-ish head reads
as a mannequin the moment its mouth moves wrong. JARVIS in the films is not
a face either: it is light, rings and a voice. So this is a particle sphere
whose surface is moved by his own voice, inside thin HUD rings with ticks.

  listening -- slow breathing, the surface barely ripples, rings drift;
  thinking  -- the particles swirl into bands, the arcs race, a scan sweeps;
  speaking  -- the surface pulses with the live TTS level (lipsync_bridge),
               the core burns amber-white and a waveform ring traces the voice;
  sleeping  -- everything contracts into a dim ember that breathes slowly.
play_assembly() streams the particles out of the ember into the sphere and
draws the rings in -- on launch and on every wake.

Same interface as hud_head3d.HeadPanel (place / top_left / set_status /
set_watching / play_assembly), so hud_bar.py can use either. The sphere is
real 3D (numpy: points on a sphere, displaced, rotated, projected, additive
splats); the rings are vector QPainter strokes on top.
"""

from __future__ import annotations

import math
import time
from collections import deque

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPen, QRadialGradient
from PyQt6.QtWidgets import QApplication, QWidget

import lipsync_bridge

WIN_W, WIN_H = 300, 300
MARGIN_R, MARGIN_B = 14, 6
CX, CY = WIN_W / 2, WIN_H / 2 + 6
R0 = 70.0                       # sphere radius, px
_N = 2600                       # surface particles (plus the globe wireframe)
_N_IN = 700                     # inner core shell
_ORBIT_N = 170                  # particles per orbit
_DUST = 150
_TICK_MS = 16
_IDLE_TICK_MS = 66   # ~15 fps once the core has burned down to the sleeping ember
_AUDIO_LATENCY_S = 0.07
_CAM_D = 4.0                    # perspective, in sphere radii

HOLO = np.array([0.35, 0.82, 1.00], np.float32)     # #59D1FF
ICE = np.array([0.85, 0.96, 1.00], np.float32)
AMBER = np.array([1.00, 0.62, 0.20], np.float32)
EMBER = np.array([1.00, 0.86, 0.60], np.float32)

LABELS = {"booting": "Запуск", "listening": "Слушаю", "thinking": "Думаю", "speaking": "Говорю",
          "sleeping": "Сон — F10 или «Hey Jarvis»"}


def _fib_sphere(n: int) -> np.ndarray:
    i = np.arange(n, dtype=np.float32)
    y = 1 - 2 * (i + 0.5) / n
    r = np.sqrt(1 - y * y)
    th = i * 2.399963
    return np.stack([np.cos(th) * r, y, np.sin(th) * r], axis=1).astype(np.float32)


def _kernel(size: int, sigma: float):
    ax = np.arange(size) - size // 2
    dx, dy = np.meshgrid(ax, ax)
    w = np.exp(-(dx ** 2 + dy ** 2) / (2 * sigma * sigma)).astype(np.float32)
    return dx.ravel(), dy.ravel(), (w / w.max()).ravel()


def _ss(x):
    x = np.clip(x, 0, 1)
    return x * x * (3 - 2 * x)


def _rotate(x, y, z, yaw: float, tilt: float):
    """Spin around the vertical axis, then tip toward the viewer."""
    cy_, sy_ = math.cos(yaw), math.sin(yaw)
    ct, st = math.cos(tilt), math.sin(tilt)
    x1 = x * cy_ + z * sy_
    z1 = -x * sy_ + z * cy_
    return x1, y * ct - z1 * st, y * st + z1 * ct


def _globe_grid() -> np.ndarray:
    """Dots along 12 meridians and 5 parallels -- the hologram-globe wireframe."""
    pts = []
    for m in range(12):
        lon = m * math.pi / 6
        lat = np.linspace(-math.pi / 2 + 0.08, math.pi / 2 - 0.08, 46)
        pts.append(np.stack([np.cos(lat) * np.cos(lon), np.sin(lat), np.cos(lat) * np.sin(lon)], 1))
    for lat in (-1.05, -0.52, 0.0, 0.52, 1.05):
        n = int(110 * math.cos(lat))
        lon = np.linspace(0, 2 * math.pi, n, endpoint=False)
        pts.append(np.stack([math.cos(lat) * np.cos(lon), np.full(n, math.sin(lat)),
                             math.cos(lat) * np.sin(lon)], 1))
    return np.concatenate(pts).astype(np.float32)


def _box_blur(a: np.ndarray, r: int) -> np.ndarray:
    """Separable box blur (two cumsum passes), edges clamped."""
    n = 2 * r + 1
    p = np.pad(a, ((r + 1, r), (0, 0), (0, 0)), mode="edge").cumsum(0)
    a = (p[n:] - p[:-n]) / n
    p = np.pad(a, ((0, 0), (r + 1, r), (0, 0)), mode="edge").cumsum(1)
    return (p[:, n:] - p[:, :-n]) / n


class _Splats:
    """Additive point splatting into a float canvas, with a fading trail and bloom."""

    def __init__(self, scale: float) -> None:
        self.S = scale
        self.cw, self.ch = int(round(WIN_W * scale)), int(round(WIN_H * scale))
        self.k = _kernel(5, 0.62 * scale)            # crisp dots
        self.k_big = _kernel(11, 1.9 * scale)        # sparkles / comet heads
        self.canvas = np.zeros((self.ch, self.cw, 3), np.float32)
        self._out = np.empty((self.ch, self.cw, 4), np.uint8)

    def _splat(self, kernel, x, y, inten, col) -> None:
        c, S, cw, ch = self.canvas, self.S, self.cw, self.ch
        kdx, kdy, kw = kernel
        m = int(kdx.max()) + 1
        xi = np.round(x * S).astype(np.int32)
        yi = np.round(y * S).astype(np.int32)
        keep = (inten > 0.004) & (xi >= m) & (xi < cw - m) & (yi >= m) & (yi < ch - m)
        if keep.any():
            flat = ((yi[keep][:, None] + kdy) * cw + (xi[keep][:, None] + kdx)).ravel()
            w = inten[keep][:, None] * kw
            cc = col[keep]
            for k in range(3):
                c[..., k] += np.bincount(flat, weights=(w * cc[:, k:k + 1]).ravel(),
                                         minlength=cw * ch).reshape(ch, cw)

    def render(self, x, y, inten, col, trail: float, big=None) -> QImage:
        c = self.canvas
        c *= trail
        self._splat(self.k, x, y, inten, col)
        if big is not None:
            self._splat(self.k_big, *big)
        # bloom: blur a 1/3-size copy and add it back, so bright areas bleed light
        d, ch, cw = 3, self.ch // 3 * 3, self.cw // 3 * 3
        small = (c[0:ch:d, 0:cw:d] + c[1:ch:d, 1:cw:d] + c[2:ch:d, 2:cw:d] + c[1:ch:d, 0:cw:d]) * 0.25
        small = _box_blur(_box_blur(small, 3), 3)
        v = c * 1.5
        v[:ch, :cw] += np.repeat(np.repeat(small, d, 0), d, 1) * 2.2
        v = v / (1.0 + v)
        np.multiply(v, 255, out=v)
        out = self._out
        out[..., :3] = v
        np.maximum(np.maximum(out[..., 0], out[..., 1]), out[..., 2], out=out[..., 3])
        img = QImage(out.data, self.cw, self.ch, 4 * self.cw, QImage.Format.Format_RGBA8888_Premultiplied).copy()
        img.setDevicePixelRatio(self.S)
        return img

    def clear(self) -> None:
        self.canvas[:] = 0


class CorePanel(QWidget):
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
        self._r = _Splats(min(dpr, 1.5))
        self._rx = lipsync_bridge.Receiver()
        self._queue: deque = deque()

        # shell = dense particle surface + the globe wireframe, displaced together
        grid = _globe_grid()
        self._n_fib = _N
        self.P = np.concatenate([_fib_sphere(_N), grid])
        n = len(self.P)
        rng = np.random.default_rng(7)
        self._seed = rng.random(n).astype(np.float32)
        self._lat = np.arcsin(np.clip(self.P[:, 1], -1, 1))       # -pi/2..pi/2
        self._lon = np.arctan2(self.P[:, 2], self.P[:, 0])
        # static "continents": smooth pseudo-noise on the sphere, brighter regions
        lat, lon = self._lat, self._lon
        noise = (np.sin(3 * lat + 1.3) * np.cos(2 * lon) + 0.6 * np.sin(5 * lat - 2 * lon + 0.7)
                 + 0.4 * np.cos(7 * lon + 3 * lat))
        self._land = _ss((noise - 0.15) / 0.7).astype(np.float32)
        self._weight = np.ones(n, np.float32)
        self._weight[_N:] = 0.9
        self._land[_N:] = 0.0
        self._sparkle = rng.choice(_N, 36, replace=False)
        self._spark_rate = rng.uniform(0.6, 1.6, 36).astype(np.float32)
        self._spark_phase = rng.uniform(0, 2 * math.pi, 36).astype(np.float32)

        # inner core: a smaller shell spinning the other way
        self.Pin = _fib_sphere(_N_IN) * 0.42
        # three tilted orbits, each with a comet head and a fading tail
        phi = np.linspace(0, 2 * math.pi, _ORBIT_N, endpoint=False).astype(np.float32)
        self._orbit_phi = phi
        self._orbit_tail = (0.10 + np.exp(-phi / 1.1)).astype(np.float32)
        self._orbits = ((1.24, 1.15, 0.3, 0.55), (1.32, -0.75, 1.9, -0.4), (1.40, 0.35, 3.6, 0.32))
        # dust drifting around the core
        d = rng.normal(size=(_DUST, 3)).astype(np.float32)
        self._dust = d / np.linalg.norm(d, axis=1, keepdims=True)
        self._dust_r = rng.uniform(1.3, 1.85, _DUST).astype(np.float32)
        self._dust_seed = rng.random(_DUST).astype(np.float32)

        self._status = "booting"        # booting | listening | thinking | speaking | sleeping
        self._watching = False
        self._last_audio = 0.0
        self._level = self._sib = 0.0
        self._energy = self._think = 0.0
        self._awake = 0.0               # 0 = ember (asleep / booting), 1 = full core
        self._t = 0.0
        self._talking = False
        self._wave: deque = deque([0.0] * 96, maxlen=96)   # recent voice levels for the waveform ring

        self._mode = "ember"            # ember | assemble | shown | dissolve
        self._mode_t0 = time.monotonic()
        self._progress = 0.0
        self._fx: QImage | None = None
        import ui_fonts

        self._tiny_font = QFont(ui_fonts.UI_FONT, 5)

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
        elif status != "sleeping" and was == "sleeping" and self._mode in ("ember", "dissolve"):
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
        self._talking = self._status == "speaking" or (now - self._last_audio) < 0.4
        lvl = self._level if self._talking else 0.0
        self._energy += (lvl - self._energy) * (0.5 if lvl > self._energy else 0.12)
        self._think += ((1.0 if self._status == "thinking" else 0.0) - self._think) * 0.06
        self._wave.append(lvl)
        self._t += (self._timer.interval() / 1000) * (0.35 + 0.65 * self._awake)

        t = now - self._mode_t0
        if self._mode == "assemble":
            self._progress = float(_ss(t / 1.6))
            self._awake = self._progress
            if t >= 1.7:
                self._mode, self._progress, self._awake = "shown", 1.0, 1.0
        elif self._mode == "dissolve":
            self._progress = 1.0 - float(_ss(t / 2.2))
            self._awake = self._progress
            if t >= 2.3:
                self._mode, self._progress, self._awake = "ember", 0.0, 0.0
        elif self._mode == "shown":
            self._progress, self._awake = 1.0, 1.0
        else:
            self._progress, self._awake = 0.0, 0.0
        self._fx = self._render()
        self.update()
        # Asleep as a dim ember: a slow breath doesn't need 60 redraws a second.
        want = _IDLE_TICK_MS if self._status == "sleeping" and self._mode == "ember" else _TICK_MS
        if self._timer.interval() != want:
            self._timer.setInterval(want)

    def _render(self) -> QImage:
        t, e, th, a = self._t, self._energy, self._think, self._awake
        P, lat, lon, seed = self.P, self._lat, self._lon, self._seed
        n_sh = len(P)

        # --- shell: slow breathing ripples + voice-driven bands
        calm = 0.035 * np.sin(3.0 * lat + t * 1.2) * np.sin(2.0 * lon - t * 0.8)
        voice = e * (0.09 * np.sin(6.0 * lat - t * 9.0) + 0.05 * np.sin(4.0 * lon + t * 6.0) + 0.05) \
            + e * self._sib * 0.04 * np.sin(11.0 * lat + t * 14.0)
        breath = 0.02 * math.sin(t * 1.4)
        # thinking: particles gather into swirling latitude bands
        r = 1.0 + calm + voice + breath + th * 0.05 * np.sin(8.0 * lat + t * 5.0)

        # rotation: steady spin, faster while thinking; a slight tilt so the poles read as 3D
        yaw = t * (0.35 + 1.4 * th)
        tilt = 0.42
        x1, y2, z2 = _rotate(P[:, 0] * r, P[:, 1] * r, P[:, 2] * r, yaw, tilt)

        # light: fresnel rim (edge-on points glow) + front bias; the back shows faintly
        front = np.clip(-z2, -1, 1)
        rim = 1.0 - np.abs(z2) / np.maximum(r, 1e-3)
        land = self._land
        inten = (0.07 + 0.38 * rim ** 2.5 + 0.16 * np.clip(front, 0, 1)) * (0.4 + 1.25 * land)
        inten = np.where(z2 > 0.15, inten * 0.3, inten)
        # a scan band sweeps up and down over the globe (faster while thinking)
        scan = math.sin(t * (0.45 + 1.6 * th)) * 1.05
        inten = inten * (1.0 + 1.3 * np.exp(-((y2 - scan) / 0.07) ** 2))
        twinkle = 0.8 + 0.2 * np.sin(t * 3.0 + seed * 40)
        inten = inten * twinkle * self._weight

        col = np.empty((n_sh, 3), np.float32)
        col[:] = HOLO
        col += (ICE - HOLO) * np.clip(rim ** 3 * 0.8 + th * 0.5 + land * 0.5, 0, 1)[:, None]
        col[self._n_fib:] = col[self._n_fib:] * 0.5 + ICE * 0.5         # wireframe reads paler
        # speaking: the inner/front particles catch the amber core
        heat = np.clip(e * 1.6 * (1 - rim) * np.clip(front + 0.3, 0, 1), 0, 1)
        col += (AMBER - col) * heat[:, None]

        # --- inner core: counter-rotating, pulses with the voice, warmer
        rin = 1.0 + 0.35 * e + 0.04 * math.sin(t * 2.3)
        Pin = self.Pin
        ix, iy, iz = _rotate(Pin[:, 0] * rin, Pin[:, 1] * rin, Pin[:, 2] * rin, -yaw * 1.7 + 0.8, tilt + 0.5)
        irim = 1.0 - np.abs(iz) / (0.42 * rin)
        i_int = (0.04 + 0.2 * irim ** 2) * (1 + 1.5 * e)
        i_col = np.empty((len(Pin), 3), np.float32)
        i_col[:] = HOLO * (1 - min(1.0, e * 1.5)) + AMBER * min(1.0, e * 1.5)

        # --- orbits: comet heads racing around tilted rings
        ox, oy, oz, o_int, o_col = [], [], [], [], []
        heads = []
        ospd = 0.55 + 2.2 * th + 0.8 * e
        for k, (rr, inc, node, spd) in enumerate(self._orbits):
            # the tail (growing phi) always lies behind the head, whichever way it runs
            ph = -math.copysign(1.0, spd) * self._orbit_phi + t * spd * ospd * 2.2
            rr = rr * (1 + 0.04 * e)
            lx, lz = np.cos(ph) * rr, np.sin(ph) * rr
            # tip the ring plane by its inclination, then turn it to its node
            ly = lz * math.sin(inc)
            lz = lz * math.cos(inc)
            cn, sn = math.cos(node + t * 0.07), math.sin(node + t * 0.07)
            gx, gz = lx * cn + lz * sn, -lx * sn + lz * cn
            x_, y_, z_ = _rotate(gx, ly, gz, 0.0, tilt)
            ox.append(x_); oy.append(y_); oz.append(z_)
            o_int.append(self._orbit_tail * np.where(z_ > 0.2, 0.35, 1.0) * 0.55)
            c = np.empty((_ORBIT_N, 3), np.float32)
            c[:] = HOLO * 0.6 + ICE * 0.4
            c[:12] = AMBER * e + ICE * (1 - e) if e > 0.05 else ICE
            o_col.append(c)
            heads.append((x_[0], y_[0], z_[0]))
        ox, oy, oz = np.concatenate(ox), np.concatenate(oy), np.concatenate(oz)
        o_int, o_col = np.concatenate(o_int), np.concatenate(o_col)

        # --- dust: slow drift, faint twinkle
        D, ds = self._dust, self._dust_seed
        dr = self._dust_r * (1 + 0.05 * np.sin(t * 0.7 + ds * 20)) * (1 + 0.15 * e)
        dx, dy, dz = _rotate(D[:, 0] * dr, D[:, 1] * dr, D[:, 2] * dr, yaw * 0.4, tilt)
        d_int = (0.05 + 0.12 * np.clip(np.sin(t * 1.3 + ds * 50), 0, 1) ** 4) * np.where(dz > 0, 0.5, 1.0)
        d_col = np.empty((_DUST, 3), np.float32)
        d_col[:] = HOLO

        X = np.concatenate([x1, ix, ox, dx])
        Y = np.concatenate([y2, iy, oy, dy])
        Z = np.concatenate([z2, iz, oz, dz])
        I = np.concatenate([inten, i_int, o_int, d_int]).astype(np.float32)
        C = np.concatenate([col, i_col, o_col, d_col])

        size = 0.22 + 0.78 * a                    # ember -> full sphere
        kz = _CAM_D / (_CAM_D + Z)
        sx = CX + R0 * size * X * kz
        sy = CY + R0 * size * Y * kz

        # assembly: particles stream out of the ember on a spiral, staggered by latitude
        if self._mode in ("assemble", "dissolve"):
            order = np.concatenate([(lat + math.pi / 2) / math.pi,
                                    np.full(len(X) - n_sh, 1.0, np.float32)])
            ang0 = np.concatenate([lon, np.linspace(0, 40, len(X) - n_sh)])
            p = _ss((self._progress * 1.35 - order * 0.35))
            ang = ang0 + (1 - p) * 4.0
            ex = CX + np.cos(ang) * (1 - p) * 18
            ey = CY + np.sin(ang) * (1 - p) * 18
            sx = ex + (sx - ex) * p
            sy = ey + (sy - ey) * p
            I = I * np.where(order >= 1.0, p, 1.0)

        I = I * (0.30 + 0.70 * a) * (1 + 1.4 * e)
        if a < 1:                                 # asleep: a warm dim ember
            C = C + (EMBER - C) * (1 - a) * 0.6

        # sparkles + comet heads: a few big soft glints
        big = None
        if a > 0.05:
            sp = self._sparkle
            flick = np.clip(np.sin(t * self._spark_rate + self._spark_phase), 0, 1) ** 14
            flick = flick * (z2[sp] < 0) * (0.9 + 1.2 * e)
            hx = np.array([h[0] for h in heads], np.float32)
            hy = np.array([h[1] for h in heads], np.float32)
            hz = np.array([h[2] for h in heads], np.float32)
            bx = np.concatenate([x1[sp], hx]); by = np.concatenate([y2[sp], hy])
            bz = np.concatenate([z2[sp], hz])
            bk = _CAM_D / (_CAM_D + bz)
            bi = np.concatenate([flick, np.where(hz > 0.2, 0.18, 0.5) * (1 + e)]).astype(np.float32) * a
            bc = np.empty((len(bi), 3), np.float32)
            bc[:] = ICE
            if e > 0.05:
                bc[len(sp):] = AMBER * min(1.0, e * 1.5) + ICE * (1 - min(1.0, e * 1.5))
            big = (CX + R0 * size * bx * bk, CY + R0 * size * by * bk, bi, bc)

        trail = 0.55 if self._mode in ("assemble", "dissolve") else 0.30
        return self._r.render(sx, sy, I, C.astype(np.float32), trail, big)

    # ------------------------------------------------------------------ paint

    def paintEvent(self, _) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        a, e, th, t = self._awake, self._energy, self._think, self._t

        # a soft dark halo only while awake, so the glow reads over bright windows
        if a > 0.02:
            back = QRadialGradient(QPointF(CX, CY), WIN_W * 0.5)
            back.setColorAt(0.0, QColor(4, 12, 26, int(170 * a)))
            back.setColorAt(0.65, QColor(4, 12, 26, int(90 * a)))
            back.setColorAt(1.0, QColor(4, 12, 26, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(back)
            p.drawEllipse(QPointF(CX, CY), WIN_W * 0.5, WIN_H * 0.5)

        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        # core glow: cyan at rest, amber-white while he speaks, a slow ember while asleep
        size = 0.22 + 0.78 * a
        rc = R0 * size * (0.9 + 0.35 * e)
        core = QRadialGradient(QPointF(CX, CY), rc)
        if a < 0.05:
            pulse = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(t * 1.6))
            core.setColorAt(0.0, QColor(255, 230, 190, int(210 * pulse)))
            core.setColorAt(0.35, QColor(255, 150, 60, int(120 * pulse)))
            core.setColorAt(1.0, QColor(255, 120, 40, 0))
        else:
            hot = min(1.0, e * 1.8)
            core.setColorAt(0.0, QColor(int(120 + 135 * hot), int(210 + 30 * hot), int(255 - 70 * hot),
                                        int(a * (70 + 150 * hot))))
            core.setColorAt(0.5, QColor(int(40 + 215 * hot), int(140 + 20 * hot), int(255 - 185 * hot),
                                        int(a * (25 + 80 * hot))))
            core.setColorAt(1.0, QColor(40, 140, 255, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(core)
        p.drawEllipse(QPointF(CX, CY), rc, rc)

        if a > 0.02:
            self._paint_rings(p, a, e, th, t, back=True)    # the far half passes behind the sphere
        if self._fx is not None:
            p.drawImage(QRectF(0, 0, WIN_W, WIN_H), self._fx)
        if a > 0.02:
            self._paint_rings(p, a, e, th, t, back=False)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        self._paint_label(p)

    def _paint_rings(self, p: QPainter, a: float, e: float, th: float, t: float, back: bool) -> None:
        """Thin HUD rings in perspective: ticks, racing arcs and the voice trace.
        Called twice -- back=True paints only the far half of every ring (dimmer,
        before the sphere), back=False the near half over it, so the rings
        visibly pass behind the core."""
        tilt = 0.30                                  # vertical squash of the ring plane
        draw_in = a                                  # rings sweep in with the assembly
        fade = 0.4 if back else 1.0

        def on_side(ang: float) -> bool:            # far half = the upper half of the ellipse
            return (math.sin(ang) < 0) == back

        def ring_pts(radius: float, a0: float, a1: float, n: int = 64, wobble=None):
            pts = []
            for i in range(n + 1):
                ang = a0 + (a1 - a0) * i / n
                rr = radius + (wobble(i / n) if wobble else 0.0)
                pts.append((QPointF(CX + math.cos(ang) * rr, CY + math.sin(ang) * rr * tilt), ang))
            return pts

        def stroke(pts, color: QColor, width: float):
            color.setAlpha(int(color.alpha() * fade))
            pen = QPen(color, width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            for (p0, a0), (p1, a1) in zip(pts, pts[1:]):
                if on_side((a0 + a1) / 2):
                    p.drawLine(p0, p1)

        # outer ring with ticks every 10 degrees, slowly turning
        rot = t * (0.15 + 1.2 * th)
        stroke(ring_pts(R0 * 1.62, rot, rot + 2 * math.pi * draw_in, 96), QColor(90, 200, 255, int(70 * a)), 1.0)
        for i in range(0, 36):
            ang = rot + i * math.pi / 18
            if ang - rot > 2 * math.pi * draw_in:
                break
            if not on_side(ang):
                continue
            long = i % 9 == 0
            r1, r2 = R0 * 1.62, R0 * (1.72 if long else 1.67)
            p.setPen(QPen(QColor(120, 215, 255, int((150 if long else 70) * a * fade)), 1.0))
            p.drawLine(QPointF(CX + math.cos(ang) * r1, CY + math.sin(ang) * r1 * tilt),
                       QPointF(CX + math.cos(ang) * r2, CY + math.sin(ang) * r2 * tilt))
            if long and not back:                    # bearing readouts on the near side
                p.setFont(self._tiny_font)
                p.setPen(QColor(130, 215, 255, int(120 * a)))
                lx, ly = CX + math.cos(ang) * R0 * 1.84, CY + math.sin(ang) * R0 * 1.84 * tilt
                p.drawText(QRectF(lx - 14, ly - 5, 28, 10), Qt.AlignmentFlag.AlignCenter, f"{i * 10:03d}")

        # a segmented ring further out, turning the other way
        seg_rot = -t * (0.08 + 0.5 * th)
        for s in range(24):
            if s / 24 > draw_in:
                break
            a0 = seg_rot + s * math.pi / 12
            alpha = 75 if s % 6 == 0 else 38
            stroke(ring_pts(R0 * 1.98, a0, a0 + math.pi / 12 * 0.62, 6),
                   QColor(90, 200, 255, int(alpha * a)), 2.2 if s % 6 == 0 else 1.0)

        # a fine dotted ring hugging the sphere
        dot_pen = QColor(140, 225, 255, int(60 * a * fade))
        pen = QPen(dot_pen, 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        for i in range(72):
            ang = -rot * 0.6 + i * math.pi / 36
            if on_side(ang):
                p.drawPoint(QPointF(CX + math.cos(ang) * R0 * 1.1, CY + math.sin(ang) * R0 * 1.1 * tilt))

        # two racing arcs on an inner ring (fast while thinking)
        spd = 0.6 + 3.2 * th
        for k, (length, off) in enumerate(((1.1, 0.0), (0.5, math.pi))):
            a0 = (t * spd * (1 if k == 0 else -1.3)) + off
            col = QColor(210, 245, 255, int(170 * a)) if th > 0.5 else QColor(100, 210, 255, int(140 * a))
            stroke(ring_pts(R0 * 1.36, a0, a0 + length * draw_in, 24), col, 2.0)

        # voice trace: a ring whose radius follows the last ~1.5 s of speech
        if e > 0.02 or any(v > 0.02 for v in self._wave):
            wave = list(self._wave)
            n = len(wave)

            def wob(u: float) -> float:
                v = wave[min(n - 1, int(u * (n - 1)))]
                return v * 9 * math.sin(u * math.pi * 24 + t * 6)

            stroke(ring_pts(R0 * 1.2, -math.pi / 2, 3 * math.pi / 2, 120, wob),
                   QColor(255, 190, 110, int(150 * a * min(1.0, 0.3 + e * 2))), 1.2)

    def _paint_label(self, p: QPainter) -> None:
        if self._mode == "assemble":
            text = f"Запуск {int(self._progress * 100)}%"
        elif self._mode in ("dissolve", "ember") and self._status == "sleeping":
            text = LABELS["sleeping"]
        elif self._status == "booting":
            text = LABELS["booting"]
        else:
            text = LABELS["speaking" if self._talking else self._status if self._status in LABELS else "listening"]
        import ui_fonts

        font = QFont(ui_fonts.UI_FONT, 8)
        p.setFont(font)
        p.setPen(QColor(150, 215, 255, 190))
        p.drawText(QRectF(0, WIN_H - 22, WIN_W - 12, 16),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, text)
        if self._watching:
            # screen capture is running (tools/screen_watch.py): always visible while it lasts
            pulse = 0.5 + 0.5 * abs(math.sin(self._t * 2.2))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 120, 110, int(140 + 110 * pulse)))
            p.drawEllipse(QPointF(14, WIN_H - 14), 3.5 + 1.5 * pulse, 3.5 + 1.5 * pulse)


def demo() -> None:
    """python hud_core.py -- ember, assembly, listening, thinking, talking, sleep."""
    import sys

    app = QApplication.instance() or QApplication(sys.argv)
    core = CorePanel()
    core._pull_audio = lambda now: None
    start = time.monotonic()
    QTimer.singleShot(800, core.play_assembly)

    def script():
        t = time.monotonic() - start
        status = ("booting" if t < 0.8 else "listening" if t < 5 else "thinking" if t < 7
                  else "speaking" if t < 14 else "listening" if t < 16 else "sleeping")
        core.set_status(status)
        if status == "speaking":
            syl = max(0.0, math.sin(t * 2 * math.pi * 3.6)) ** 0.6
            phrase = 1.0 if (t % 3.5) < 2.7 else 0.0
            core._level = 0.85 * syl * phrase
            core._sib = 1.0 if int(t * 3.6) % 5 == 2 else 0.1
            core._last_audio = time.monotonic()
        else:
            core._level = 0.0

    feeder = QTimer()
    feeder.timeout.connect(script)
    feeder.start(16)
    QTimer.singleShot(21000, app.quit)
    app.exec()


if __name__ == "__main__":
    demo()
