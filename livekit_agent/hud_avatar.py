"""AvatarPanel: Jarvis's face under the status bar, made of glowing orbs that
assemble into a face and talk.

Assets come from scripts/make_avatar.py (data/avatar/): a photoreal
head-and-shoulders portrait with a transparent background, the same portrait
with the mouth redrawn for a few visemes (half / open / round / wide) and one
with the eyes closed. Only the mouth and eye regions differ between them.

Style "orbs" (default, config.HUD_FACE_STYLE): the face *is* a cloud of
thousands of glowing orbs, one per sampled pixel of the portrait. Every orb
takes its colour and brightness from the current frame -- the base blended
with the viseme frames by the live audio (lipsync_bridge) and with the blink
frame -- so the mouth opens right inside the cloud: the dark mouth cavity
dims its orbs, teeth light them up. The orbs shimmer, twinkle and breathe;
a few larger bokeh orbs drift over the fine ones.

Style "photo": the same entrance, then the real portrait fades in and the
viseme crops are blended over it.

Entrances and exits (both styles):
  * assemble -- on launch / wake: big orbs spiral in from the edges, burst
    into sparks, the sparks fly to their places in the face (head first,
    shoulders last);
  * materialize -- every later reply: a quick stream down out of the bar;
  * dissolve -- when he stops talking: the face streams back up into the bar.
Everything particle-based is rendered with numpy into one additive-glow image
per frame.
"""

from __future__ import annotations

import json
import math
import random
import time
from collections import deque

import numpy as np
from PIL import Image, ImageFilter
from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPixmap, QRadialGradient
from PyQt6.QtWidgets import QApplication, QWidget

import config
import lipsync_bridge
from ui_colors import C, qcol

AVATAR_DIR = config.DATA_DIR / "avatar"
_FRAMES = ("half", "open", "round", "wide")

WIN_W, WIN_H = 440, 420
BUST = 330                         # face drawn BUST x BUST, centered, near the top
BUST_X, BUST_Y = (WIN_W - BUST) / 2, 6
_GAP = 6
_TICK_MS = 16
_AUDIO_LATENCY_S = 0.07
_HOLD_AFTER_SPEECH_S = 1.1
_WAIT_FOR_GREETING_S = 7.0         # after the launch assembly, stay up this long for the first words

_N_FINE = 9000                     # small orbs: the detail of the face
_N_BOKEH = 260                     # large soft orbs drifting over it
_N_ORBS = 14                       # the big orbs of the launch entrance
_PRI = np.array([0x84, 0xDF, 0xFF], dtype=np.float32) / 255.0


def assets_ready() -> bool:
    return all((AVATAR_DIR / f).exists() for f in
               ["base.png", "blink.png", "meta.json"] + [f"viseme_{n}.png" for n in _FRAMES])


def _qimage_rgba(arr: np.ndarray) -> QImage:
    """HxWx4 uint8 RGBA (straight alpha) -> QImage that owns a copy."""
    h, w = arr.shape[:2]
    img = QImage(np.ascontiguousarray(arr).data, w, h, 4 * w, QImage.Format.Format_RGBA8888)
    return img.copy()


def _smoothstep(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


class _Sprites:
    """Portrait frames scaled for the screen (arrays for the orbs, pixmaps and
    mouth/eye crops for the photo style)."""

    def __init__(self, dpr: float) -> None:
        meta = json.loads((AVATAR_DIR / "meta.json").read_text(encoding="utf-8"))
        src = meta.get("size", [1024, 1024])[0]
        px = int(round(BUST * dpr))
        self.px = px
        self.scale = px / src
        self.dpr = dpr

        fade = np.ones(px, dtype=np.float32)          # soft fade-out of the shoulders
        start = int(px * 0.74)
        fade[start:] = np.linspace(1.0, 0.0, px - start) ** 1.4

        def load(name: str) -> np.ndarray:
            im = Image.open(AVATAR_DIR / name).convert("RGBA").resize((px, px), Image.LANCZOS)
            arr = np.asarray(im).copy()
            arr[..., 3] = (arr[..., 3].astype(np.float32) * fade[:, None]).astype(np.uint8)
            return arr

        self.arr = {"base": load("base.png"), "blink": load("blink.png")}
        for n in _FRAMES:
            self.arr[n] = load(f"viseme_{n}.png")
        base = self.arr["base"]
        self.base = self._pix(base)

        glow = Image.fromarray(base[..., 3]).filter(ImageFilter.GaussianBlur(px / 28))
        g = np.zeros((px, px, 4), dtype=np.uint8)
        g[..., 0], g[..., 1], g[..., 2] = 0x84, 0xDF, 0xFF
        g[..., 3] = np.clip(np.asarray(glow, dtype=np.float32) * 1.3, 0, 255).astype(np.uint8)
        self.glow = self._pix(g)

        def crop_box(region):
            x0, y0, x1, y1 = (int(round(v * self.scale)) for v in region)
            return max(0, x0), max(0, y0), min(px, x1), min(px, y1)

        self.mouth_box = crop_box(meta["mouth_region"])
        self.eyes_box = crop_box(meta["eyes_region"])
        self.mouth = {n: self._crop(self.arr[n], self.mouth_box) for n in _FRAMES}
        self.blink = self._crop(self.arr["blink"], self.eyes_box)

    def _pix(self, arr: np.ndarray) -> QPixmap:
        pm = QPixmap.fromImage(_qimage_rgba(arr))
        pm.setDevicePixelRatio(self.dpr)
        return pm

    def _crop(self, arr: np.ndarray, box) -> QPixmap:
        x0, y0, x1, y1 = box
        return self._pix(arr[y0:y1, x0:x1])

    def logical(self, box) -> QRectF:
        x0, y0, x1, y1 = box
        return QRectF(x0 / self.dpr, y0 / self.dpr, (x1 - x0) / self.dpr, (y1 - y0) / self.dpr)


def _kernel(size: int, sigma: float):
    r = np.arange(size) - size // 2
    kx, ky = np.meshgrid(r, r)
    return kx.ravel(), ky.ravel(), np.exp(-(kx * kx + ky * ky).ravel() / (2 * sigma * sigma)).astype(np.float32)


class _Particles:
    """Orbs whose home is a pixel of the portrait. Rendered additively with
    numpy at the screen's real pixel density (S = device pixel ratio)."""

    def __init__(self, sp: _Sprites) -> None:
        self.S = S = sp.dpr
        self.cw, self.ch = int(round(WIN_W * S)), int(round(WIN_H * S))
        rng = np.random.default_rng(3)
        px = sp.px
        base = sp.arr["base"].astype(np.float32) / 255.0
        alpha = base[..., 3]
        lum = base[..., :3].mean(axis=2)
        gy, gx = np.gradient(lum)
        edge = np.sqrt(gx * gx + gy * gy)

        # fine orbs favour detail (edges, bright skin) and the face over the shoulders;
        # bokeh orbs spread evenly
        face_bias = np.clip(1.25 - np.linspace(0, 1, px), 0.25, 1.0)[:, None]
        w_fine = (alpha * face_bias * (0.2 + lum + 6.0 * edge)).ravel()
        fine = rng.choice(w_fine.size, size=_N_FINE, replace=False, p=w_fine / w_fine.sum())
        # extra orbs where the face moves: the mouth reads best with a dense patch
        def patch(box, count):
            x0, y0, x1, y1 = box
            yy, xx = np.mgrid[y0:y1, x0:x1]
            cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
            inside = ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2 < 1.0
            cand = (yy * px + xx)[inside]
            return rng.choice(cand, size=min(count, cand.size), replace=False)

        extra = np.concatenate([patch(sp.mouth_box, 1700), patch(sp.eyes_box, 700)])
        w_bok = (alpha > 0.4).ravel().astype(np.float64)
        bok = rng.choice(w_bok.size, size=_N_BOKEH, replace=False, p=w_bok / w_bok.sum())
        idx = np.concatenate([fine, extra, bok])
        self.n = idx.size
        self.is_bokeh = np.zeros(self.n, dtype=bool)
        self.is_bokeh[-_N_BOKEH:] = True
        py, pxx = np.divmod(idx, px)
        self.tx = (BUST_X + pxx / sp.dpr).astype(np.float32)          # logical coordinates
        self.ty = (BUST_Y + py / sp.dpr).astype(np.float32)

        # jaw rig: how much each orb follows the jaw when the mouth opens --
        # full below the lip line inside the mouth area, fading out around it
        mx0, my0, mx1, my1 = (v / sp.dpr for v in sp.mouth_box)
        mcx = BUST_X + (mx0 + mx1) / 2
        lips_y = BUST_Y + my0 + (my1 - my0) * 0.28                   # between the lips (region is padded below)
        rx, ry = (mx1 - mx0) * 0.62, (my1 - my0) * 0.95
        d = np.sqrt(((self.tx - mcx) / rx) ** 2 + ((self.ty - lips_y) / ry) ** 2)
        falloff = np.clip(1.25 - d, 0.0, 1.0) ** 1.5
        below = np.clip((self.ty - lips_y) / 6.0 + 0.5, 0.0, 1.0)
        self.jaw = (falloff * below).astype(np.float32)
        self.upper_lip = (falloff * (1.0 - below) * np.clip(1.0 - (lips_y - self.ty) / 14.0, 0, 1)).astype(np.float32)

        # colour + brightness of every orb in every frame, so blending frames
        # per tick is just a weighted sum of small arrays
        self.frame_col: dict[str, np.ndarray] = {}
        self.frame_int: dict[str, np.ndarray] = {}
        for name, arr in sp.arr.items():
            # local contrast: skin is nearly one flat brightness, so the orbs
            # take an unsharp-masked luminance -- eyes, brows, nostrils, lips
            # and the open mouth read clearly in the cloud
            lum_full = arr[..., :3].astype(np.float32) @ np.array([0.3, 0.55, 0.15], dtype=np.float32) / 255.0
            blur = np.asarray(Image.fromarray((lum_full * 255).astype(np.uint8)).filter(
                ImageFilter.GaussianBlur(px / 55)), dtype=np.float32) / 255.0
            sharp = np.clip(lum_full + 2.2 * (lum_full - blur), 0.0, 1.0)
            a = arr[py, pxx].astype(np.float32) / 255.0
            rgb, al = a[:, :3], a[:, 3]
            s = sharp[py, pxx]
            self.frame_col[name] = (0.3 * rgb + _PRI * (0.55 + 0.75 * s[:, None]) * 0.7).astype(np.float32)
            self.frame_int[name] = (al * (0.04 + 1.9 * s ** 1.6)).astype(np.float32)
        self.col = self.frame_col["base"]
        self.size = rng.uniform(0.6, 1.2, self.n).astype(np.float32)
        self.size[self.is_bokeh] *= 0.35
        self.phase = rng.uniform(0, 2 * np.pi, self.n).astype(np.float32)
        self.freq = rng.uniform(0.6, 2.2, self.n).astype(np.float32)

        self.k_fine = _kernel(5, 0.85 * S)
        self.k_bokeh = _kernel(int(2 * round(4 * S) + 1), 2.6 * S)
        self.canvas = np.zeros((self.ch, self.cw, 3), dtype=np.float32)

    def render(self, x, y, intensity, col=None, trail: float = 0.72) -> QImage:
        """x, y in logical window coordinates; one entry per orb (or none)."""
        c = self.canvas
        c *= trail
        col = self.col if col is None else col
        if np.size(x):
            S, cw, ch = self.S, self.cw, self.ch
            xi = np.round(np.asarray(x) * S).astype(np.int32)
            yi = np.round(np.asarray(y) * S).astype(np.int32)
            for mask, (kdx, kdy, kw) in ((~self.is_bokeh, self.k_fine), (self.is_bokeh, self.k_bokeh)):
                pad = int(np.abs(kdx).max()) + 1
                keep = mask & (xi >= pad) & (xi < cw - pad) & (yi >= pad) & (yi < ch - pad) & (intensity > 0.004)
                if not keep.any():
                    continue
                flat = ((yi[keep][:, None] + kdy) * cw + (xi[keep][:, None] + kdx)).ravel()
                inten = intensity[keep][:, None] * kw
                cc = col[keep]
                for chn in range(3):
                    c[..., chn] += np.bincount(flat, weights=(inten * cc[:, chn:chn + 1]).ravel(),
                                               minlength=cw * ch).reshape(ch, cw)
        g = 1.9 * c
        rgb = g / (1.0 + g)                                # soft tone-map: dense areas bloom instead of clipping
        out = np.empty((self.ch, self.cw, 4), dtype=np.uint8)
        out[..., :3] = (rgb * 255).astype(np.uint8)        # premultiplied: colour already carries its brightness
        out[..., 3] = out[..., :3].max(axis=2)
        img = QImage(out.data, self.cw, self.ch, 4 * self.cw, QImage.Format.Format_RGBA8888_Premultiplied)
        img = img.copy()
        img.setDevicePixelRatio(self.S)
        return img

    def clear(self) -> None:
        self.canvas[:] = 0


class AvatarPanel(QWidget):
    def __init__(self, anchor, parent=None, style: str | None = None):
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
        self.setFixedSize(WIN_W, WIN_H)
        self._anchor = anchor
        self._style = style or config.HUD_FACE_STYLE

        dpr = QApplication.primaryScreen().devicePixelRatio()
        self._sp = _Sprites(dpr)
        self._pt = _Particles(self._sp)
        self._rng = np.random.default_rng()

        self._rx = lipsync_bridge.Receiver()
        self._queue: deque = deque()
        self._speaking = False
        self._last_audio = 0.0
        self._level = self._sib = 0.0
        self._open = self._wide = self._energy = 0.0
        self._vowel = "open"
        self._armed = True            # ready to pick a new vowel shape on the next syllable
        self._t = 0.0

        self._blink = 0.0
        self._blink_start = None
        self._next_blink = time.monotonic() + random.uniform(1.5, 4.0)

        # show state machine: hidden -> (assemble|materialize) -> shown -> dissolve -> hidden
        self._mode = "hidden"
        self._mode_t0 = 0.0
        self._linger_until = 0.0
        self._fx: QImage | None = None
        self._reveal = 0.0            # photo style: how much of the real portrait is visible
        self._orbs_t = None

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._step)
        self._timer.start(_TICK_MS)

    # ------------------------------------------------------------------ control

    def set_speaking(self, speaking: bool) -> None:
        self._speaking = speaking

    def play_assembly(self) -> None:
        """The full launch entrance; stays up a while for the greeting."""
        self._start("assemble")
        self._linger_until = time.monotonic() + _WAIT_FOR_GREETING_S

    def _start(self, mode: str) -> None:
        self._mode, self._mode_t0 = mode, time.monotonic()
        rng, n, pt = self._rng, self._pt.n, self._pt
        self._pt.clear()
        yrel = (pt.ty - BUST_Y) / BUST
        self._curl = rng.uniform(-1, 1, n).astype(np.float32)
        if mode == "assemble":
            self._orb_ang0 = rng.uniform(0, 2 * math.pi, _N_ORBS)
            self._orb_r0 = rng.uniform(260, 330, _N_ORBS)
            self._orb_spin = rng.choice([-1.0, 1.0], _N_ORBS) * rng.uniform(1.6, 2.6, _N_ORBS)
            self._orb_of = rng.integers(0, _N_ORBS, n)
            # head forms first: delay grows with the target's height in the bust
            self._delay = (0.05 + 0.55 * yrel + rng.uniform(0, 0.18, n)).astype(np.float32)
            self._jit = rng.normal(0, 14, (n, 2)).astype(np.float32)
        else:
            # from / to the bar's centre, just above the window's top edge
            self._src = np.array([WIN_W / 2, -8], dtype=np.float32)
            self._delay = (0.35 * yrel + rng.uniform(0, 0.12, n)).astype(np.float32)
            self._jit = rng.normal(0, 30, (n, 2)).astype(np.float32)
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
        self._animate_mouth(talking)
        self._step_blink(now)
        self._step_fx(now)
        self.update()

    def _animate_mouth(self, talking: bool) -> None:
        tgt = self._level if talking else 0.0
        self._open += (tgt - self._open) * (0.5 if tgt > self._open else 0.3)
        self._wide += (self._sib * min(1.0, self._level * 3) - self._wide) * 0.35
        self._energy += (self._level - self._energy) * 0.06
        # a new syllable picks its vowel shape: mostly "a", sometimes "o"
        if self._level < 0.15:
            self._armed = True
        elif self._armed and self._level > 0.3:
            self._armed = False
            self._vowel = "round" if random.random() < 0.3 else "open"

    def _mouth_weights(self) -> list[tuple[str, float]]:
        amount = float(np.clip((self._open - 0.07) / 0.6, 0, 1))
        if amount <= 0.01:
            return []
        wide = float(np.clip(self._wide * 1.4, 0, 1)) * min(1.0, amount * 2)
        half_w = min(1.0, amount / 0.4) * (1.0 - 0.7 * float(_smoothstep((amount - 0.4) / 0.5)))
        vowel_w = float(_smoothstep((amount - 0.3) / 0.5))
        rest = 1.0 - wide
        return [(f, w) for f, w in (("half", half_w * rest), (self._vowel, vowel_w * rest), ("wide", wide))
                if w > 0.02]

    def _step_blink(self, now: float) -> None:
        if self._blink_start is None and now >= self._next_blink:
            self._blink_start = now
        if self._blink_start is not None:
            p = (now - self._blink_start) / 0.17
            self._blink = math.sin(min(1.0, p) * math.pi)
            if p >= 1.0:
                self._blink_start, self._blink = None, 0.0
                self._next_blink = now + (0.2 if random.random() < 0.15 else random.uniform(2.4, 5.5))

    # ------------------------------------------------------------------ particles

    def _live_face(self):
        """Current colour/brightness of every orb: base blended with the mouth
        shapes and the blink, exactly like the photo style blends its crops."""
        pt = self._pt
        col = pt.frame_col["base"].copy()
        inten = pt.frame_int["base"].copy()
        stack = self._mouth_weights()
        if self._blink > 0.02:
            stack.append(("blink", min(1.0, self._blink * 1.25)))
        for name, w in stack:              # sequential "over", same as painting crops on top
            w = min(1.0, w)
            col += (pt.frame_col[name] - col) * w
            inten += (pt.frame_int[name] - inten) * w
        return col, inten

    def _home(self):
        """Where every orb sits right now: its pixel, plus shimmer, breathing and a slight sway."""
        pt, t = self._pt, self._t
        e = self._energy
        wob = 0.35 + 1.1 * e
        x = pt.tx + wob * np.sin(pt.phase + t * pt.freq * 3.1)
        y = pt.ty + wob * np.cos(pt.phase * 1.7 + t * pt.freq * 2.7)
        bok = pt.is_bokeh
        x[bok] += 5.0 * np.sin(pt.phase[bok] + t * 0.7)
        y[bok] += 4.0 * np.cos(pt.phase[bok] * 1.3 + t * 0.55)
        # the jaw drops with the opening, the upper lip lifts a touch; "и/с" pulls
        # the corners sideways instead of dropping as far
        amount = float(np.clip((self._open - 0.05) / 0.6, 0, 1))
        wide = float(np.clip(self._wide * 1.4, 0, 1))
        y += pt.jaw * (9.0 * amount * (1.0 - 0.55 * wide)) - pt.upper_lip * 2.0 * amount
        x += (pt.tx - (BUST_X + BUST / 2)) * 0.035 * wide * (pt.jaw + pt.upper_lip)
        # breathing + sway around the neck
        cx, cy = BUST_X + BUST / 2, BUST_Y + BUST * 0.62
        ang = math.radians(0.6 * math.sin(t * 0.8) + 0.8 * e * math.sin(t * 2.7))
        s = 1.0 + 0.005 * math.sin(t * 1.6)
        dx, dy = (x - cx) * s, (y - cy) * s
        ca, sa = math.cos(ang), math.sin(ang)
        return cx + dx * ca - dy * sa, cy + dx * sa + dy * ca + 1.2 * math.sin(t * 1.6) - 2.0 * e

    def _twinkle(self) -> np.ndarray:
        pt = self._pt
        tw = 0.78 + 0.22 * np.sin(pt.phase * 3.0 + self._t * pt.freq * 4.0)
        return (tw * (0.9 + 0.35 * self._energy)).astype(np.float32) * pt.size

    def _step_fx(self, now: float) -> None:
        t = now - self._mode_t0
        pt = self._pt
        orbs = self._style != "photo"
        if self._mode == "assemble":
            orb_end, fly, total = 1.15, 1.25, 3.0
            if t < orb_end:
                self._reveal, self._orbs_t = 0.0, t / orb_end
                self._fx = pt.render([], [], None, trail=0.8)
                return
            self._orbs_t = None
            tb = t - orb_end
            hx, hy = self._home()
            burst = self._orb_positions(1.0)
            sx = burst[self._orb_of, 0] + self._jit[:, 0]
            sy = burst[self._orb_of, 1] + self._jit[:, 1]
            p = _smoothstep((tb - self._delay) / fly)
            x, y = self._curve(sx, sy, hx, hy, p, 60)
            if orbs:
                col, inten = self._live_face()
                arriving = np.where(tb < self._delay, 0.5, 0.5 + 0.5 * p)
                bright = inten * arriving + 0.35 * (1 - p) * (tb >= self._delay)
                self._fx = pt.render(x, y, (bright * self._twinkle()).astype(np.float32), col, trail=0.55)
            else:
                inten = np.where(tb < self._delay, 0.55, 0.9) * (1.0 - 0.9 * _smoothstep((t - 2.6) / 0.7))
                self._reveal = float(_smoothstep((t - 2.15) / 0.9))
                self._fx = pt.render(x, y, (inten * pt.size).astype(np.float32), trail=0.62)
            if t >= total + (0.0 if orbs else 0.3):
                self._finish_in()
        elif self._mode == "materialize":
            fly, total = 0.55, 1.1
            hx, hy = self._home()
            p = _smoothstep((t - self._delay) / fly)
            sx = self._src[0] + self._jit[:, 0] * 0.4
            sy = np.full(pt.n, self._src[1], dtype=np.float32) + np.abs(self._jit[:, 1]) * 0.1
            x, y = self._curve(sx, sy, hx, hy, p, 45)
            if orbs:
                col, inten = self._live_face()
                bright = np.where(t < self._delay, 0.0, inten * (0.4 + 0.6 * p) + 0.3 * (1 - p))
                self._fx = pt.render(x, y, (bright * self._twinkle()).astype(np.float32), col, trail=0.5)
            else:
                inten = np.where(t < self._delay, 0.0, 0.85) * (1.0 - _smoothstep((t - 0.75) / 0.45))
                self._reveal = float(_smoothstep((t - 0.45) / 0.6))
                self._fx = pt.render(x, y, (inten * pt.size).astype(np.float32), trail=0.6)
            if t >= total:
                self._finish_in()
        elif self._mode == "dissolve":
            fly, total = 0.6, 1.1
            hx, hy = self._home()
            self._reveal = float(1.0 - _smoothstep(t / 0.35))
            d = (1.0 - (pt.ty - BUST_Y) / BUST) * 0.3        # shoulders go first, the head last
            p = _smoothstep((t - d.astype(np.float32) * 0.8) / fly)
            ex = self._src[0] + self._jit[:, 0] * 0.3
            ey = np.full(pt.n, self._src[1], dtype=np.float32)
            x = hx + (ex - hx) * p + self._curl * 50 * np.sin(p * math.pi)
            y = hy + (ey - hy) * p
            if orbs:
                col, inten = self._live_face()
                bright = (inten * (1 - p) + 0.35 * np.sin(p * math.pi)) * (1.0 - p ** 3)
                self._fx = pt.render(x, y, (bright * self._twinkle()).astype(np.float32), col, trail=0.55)
            else:
                inten = (0.9 * np.clip(t / 0.15, 0, 1) * (1.0 - p ** 3)).astype(np.float32)
                self._fx = pt.render(x, y, inten * pt.size, trail=0.6)
            if t >= total:
                self._mode, self._fx, self._reveal = "hidden", None, 0.0
                pt.clear()
                self.hide()
        else:  # shown
            if orbs:
                hx, hy = self._home()
                col, inten = self._live_face()
                self._fx = pt.render(hx, hy, (inten * self._twinkle()).astype(np.float32), col, trail=0.35)
            else:
                self._fx, self._reveal = None, 1.0

    def _finish_in(self) -> None:
        self._mode, self._reveal = "shown", 1.0
        if self._style == "photo":
            self._fx = None
            self._pt.clear()

    def _curve(self, sx, sy, hx, hy, p, bend):
        """Start -> home along a curl, so sparks swirl in instead of flying straight."""
        x = sx + (hx - sx) * p + self._curl * bend * np.sin(p * math.pi)
        y = sy + (hy - sy) * p - bend * 0.4 * np.sin(p * math.pi)
        return x, y

    def _orb_positions(self, k: float) -> np.ndarray:
        """Launch orbs spiral from far out to a ring around the face (k: 0..1)."""
        e = _smoothstep(np.float32(k))
        r = self._orb_r0 * (1 - e) + 95 * e
        a = self._orb_ang0 + self._orb_spin * e * math.pi
        cx, cy = WIN_W / 2, BUST_Y + BUST * 0.42
        return np.stack([cx + r * np.cos(a), cy + r * 0.85 * np.sin(a)], axis=1)

    # ------------------------------------------------------------------ paint

    def paintEvent(self, _) -> None:
        if self._mode == "hidden":
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        if self._style == "photo" and self._reveal > 0.001:
            self._paint_bust(p)
        if self._style != "photo" and self._mode != "hidden":
            self._paint_aura(p)
        if self._fx is not None:
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
            p.drawImage(QRectF(0, 0, WIN_W, WIN_H), self._fx)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        if self._mode == "assemble" and self._orbs_t is not None:
            self._paint_orbs(p, self._orbs_t)

    def _paint_aura(self, p: QPainter) -> None:
        """A faint halo behind the orb face that breathes with the voice."""
        if self._mode == "assemble" and self._orbs_t is not None:
            return
        cx, cy = WIN_W / 2, BUST_Y + BUST * 0.40
        # dark glass behind the face first: whatever is on the desktop
        # underneath would otherwise swallow the finer orbs
        t = time.monotonic() - self._mode_t0
        fade = {"shown": 1.0,
                "materialize": min(1.0, t / 0.8),
                "dissolve": max(0.0, 1.0 - t / 0.9),
                "assemble": float(np.clip((t - 1.15) / 1.5, 0, 1))}.get(self._mode, 0.0)
        dark = QRadialGradient(QPointF(cx, cy + 20), 190)
        dark.setColorAt(0.0, qcol(C.BG, int(215 * fade)))
        dark.setColorAt(0.6, qcol(C.BG, int(170 * fade)))
        dark.setColorAt(1.0, qcol(C.BG, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(dark)
        p.drawEllipse(QPointF(cx, cy + 20), 190, 190)
        r = 150 + 30 * self._energy
        g = QRadialGradient(QPointF(cx, cy), r)
        g.setColorAt(0.0, qcol(C.PRI, int(26 + 40 * self._energy)))
        g.setColorAt(1.0, qcol(C.PRI, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawEllipse(QPointF(cx, cy), r, r)

    def _paint_bust(self, p: QPainter) -> None:
        sp = self._sp
        bust = QImage(int(BUST * sp.dpr), int(BUST * sp.dpr), QImage.Format.Format_ARGB32_Premultiplied)
        bust.setDevicePixelRatio(sp.dpr)
        bust.fill(Qt.GlobalColor.transparent)
        b = QPainter(bust)
        b.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        b.drawPixmap(0, 0, sp.base)
        top_left = sp.logical(sp.mouth_box).topLeft()
        for frame, w in self._mouth_weights():
            b.setOpacity(min(1.0, w))
            b.drawPixmap(top_left, sp.mouth[frame])
        b.setOpacity(1.0)
        if self._blink > 0.02:
            b.setOpacity(min(1.0, self._blink * 1.25))
            b.drawPixmap(sp.logical(sp.eyes_box).topLeft(), sp.blink)
            b.setOpacity(1.0)
        # hologram: faint cyan tint + scanlines, only where the portrait is
        b.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
        b.fillRect(QRectF(0, 0, BUST, BUST), qcol(C.PRI, 16 + int(20 * self._energy)))
        y = (self._t * 14) % 3
        line = qcol(C.PRI, 16)
        while y < BUST:
            b.fillRect(QRectF(0, y, BUST, 1), line)
            y += 3
        sweep = (self._t * 70) % (BUST + 80) - 40
        g = QLinearGradient(0, sweep - 20, 0, sweep + 20)
        g.setColorAt(0, qcol(C.PRI, 0))
        g.setColorAt(0.5, qcol(C.PRI, 34))
        g.setColorAt(1, qcol(C.PRI, 0))
        b.fillRect(QRectF(0, sweep - 20, BUST, 40), g)
        b.end()

        p.save()
        cx, cy = BUST_X + BUST / 2, BUST_Y + BUST * 0.55
        p.translate(cx, cy + 1.2 * math.sin(self._t * 1.6) - 2.0 * self._energy)
        p.rotate(0.5 * math.sin(self._t * 0.8) + 0.6 * self._energy * math.sin(self._t * 2.7))
        s = 1.0 + 0.004 * math.sin(self._t * 1.6)
        p.scale(s, s)
        p.translate(-cx, -cy)
        reveal = self._reveal
        p.setOpacity((0.35 + 0.45 * self._energy) * reveal)
        p.drawPixmap(QRectF(BUST_X, BUST_Y, BUST, BUST), sp.glow, QRectF(sp.glow.rect()))
        p.setOpacity(1.0)
        if reveal < 0.999:
            edge_y = BUST_Y + BUST * reveal
            p.setClipRect(QRectF(0, 0, WIN_W, edge_y))
            p.drawImage(QRectF(BUST_X, BUST_Y, BUST, BUST), bust)
            p.setClipping(False)
            band = QLinearGradient(0, edge_y - 10, 0, edge_y + 4)
            band.setColorAt(0, qcol(C.PRI, 0))
            band.setColorAt(0.7, qcol(C.WHITE, 150))
            band.setColorAt(1, qcol(C.PRI, 0))
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
            p.fillRect(QRectF(BUST_X + 20, edge_y - 10, BUST - 40, 14), band)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        else:
            p.drawImage(QRectF(BUST_X, BUST_Y, BUST, BUST), bust)
        p.restore()

    def _paint_orbs(self, p: QPainter, k: float) -> None:
        pos = self._orb_positions(k)
        grow = float(_smoothstep(np.float32(k / 0.3)))
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        p.setPen(Qt.PenStyle.NoPen)
        for i, (x, y) in enumerate(pos):
            r = (7 + 5 * math.sin(self._t * 6 + i)) * grow * (1 + 0.6 * k)
            g = QRadialGradient(QPointF(x, y), r * 2.6)
            g.setColorAt(0.0, QColor(255, 255, 255, 255))
            g.setColorAt(0.22, qcol(C.PRI, 230))
            g.setColorAt(1.0, qcol(C.PRI, 0))
            p.setBrush(g)
            p.drawEllipse(QPointF(x, y), r * 2.6, r * 2.6)
        # a gathering core that swells as the orbs close in
        cx, cy = WIN_W / 2, BUST_Y + BUST * 0.42
        cr = 30 + 60 * k
        g = QRadialGradient(QPointF(cx, cy), cr)
        g.setColorAt(0.0, qcol(C.PRI, int(90 * k)))
        g.setColorAt(1.0, qcol(C.PRI, 0))
        p.setBrush(g)
        p.drawEllipse(QPointF(cx, cy), cr, cr)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)


def demo() -> None:
    """python hud_avatar.py [photo] -- launch assembly + fake talking, no worker needed."""
    import sys

    import ui_fonts
    from compact_bar import CompactBar, _HEIGHT

    app = QApplication.instance() or QApplication(sys.argv)
    ui_fonts.load_bundled_fonts()
    bar = CompactBar()
    bar._show_sig.emit()
    style = sys.argv[1] if len(sys.argv) > 1 else None
    avatar = AvatarPanel(lambda: (bar.x() + bar.width() / 2, bar.y() + _HEIGHT), style=style)
    avatar.play_assembly()
    start = time.monotonic()

    def fake_voice():
        # drives the panel directly (not over UDP), so it works next to a running Jarvis
        t = time.monotonic() - start
        speaking = 3.4 < t < 11
        avatar.set_speaking(speaking)
        bar._state_sig.emit("SPEAKING" if speaking else "LISTENING")
        if speaking:
            syl = max(0.0, math.sin(t * 2 * math.pi * 3.6)) ** 0.6
            phrase = 1.0 if (t % 3.5) < 2.7 else 0.0
            avatar._level = 0.8 * syl * phrase
            avatar._sib = 1.0 if int(t * 3.6) % 5 == 2 else 0.1
            avatar._last_audio = time.monotonic()

    avatar._pull_audio = lambda now: None
    feeder = QTimer()
    feeder.timeout.connect(fake_voice)
    feeder.start(16)
    QTimer.singleShot(16000, app.quit)
    app.exec()


if __name__ == "__main__":
    demo()
