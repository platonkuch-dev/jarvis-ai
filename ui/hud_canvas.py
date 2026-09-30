"""
HudCanvas: the animated particle "neural face" HUD widget.

Split out of ui.py (Stage 2 module split, see REWORK_PLAN.md) with no
behavior changes.
"""
from __future__ import annotations

import math
import random
import time

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient
from PyQt6.QtWidgets import QSizePolicy, QWidget

from ui import fonts as _fonts
from ui.colors import C, qcol, rgbcol


class HudCanvas(QWidget):
    """
    A particle-built "neural face" — replaces the old static face-image +
    breathing-halo look with an animated point-cloud head that visibly
    talks. Everything derives from two eased values, `_energy` (how active
    — idle through speaking) and `_warmth` (cyan -> amber mix, "speaking
    intensity"), computed each tick from the same public state/speaking/
    muted attributes the rest of the app already sets — no other file
    needed to change for this.
    """

    _STATE_TARGETS = {
        # state -> (energy, warmth)
        "SPEAKING":     (0.95, 0.90),
        "THINKING":     (0.55, 0.32),
        "PROCESSING":   (0.55, 0.32),
        "LISTENING":    (0.40, 0.08),
        "SLEEPING":     (0.08, 0.02),
    }
    _DEFAULT_TARGET = (0.18, 0.05)
    _MUTED_TARGET   = (0.12, 0.03)

    # Subsystem constellation — real JARVIS components orbiting the core,
    # inspired by APEX-UI's agent-graph (github.com/RubenM1990/APEX-UI).
    # Deliberately mapped to things that actually exist and actually have a
    # meaningful on/off state — not decorative labels. Status comes from
    # set_subsystem_status(), called from main.py with real values (is the
    # voice pipeline prewarmed, did Telegram actually log in, etc.); a node
    # with no status reported yet defaults to "online" rather than flashing
    # red on every startup before the real check has run.
    _NODES = [
        {"id": "fast_path",  "label": "Fast Path",
         "desc": "Local wake word + speech-to-text + router + text-to-speech. "
                  "Simple commands never leave this machine — zero network round-trip.",
         "examples": ["Громче", "Сделай скриншот", "Закрой окно"]},
        {"id": "minilm",     "label": "MiniLM",
         "desc": "Fuzzy intent classifier — catches paraphrases the exact regex "
                  "router misses, still without calling the reasoning model.",
         "examples": ["Подними звук", "Вырубай звук"]},
        {"id": "smart_path", "label": "Smart Path",
         "desc": "Gemini Live — full reasoning, multi-step tool use, vision. "
                  "Handles anything the Fast Path doesn't confidently match.",
         "examples": ["Найди последний файл и перемести на рабочий стол"]},
        {"id": "windows",    "label": "Windows Control",
         "desc": "Native Win32 + UI Automation — open/close/focus apps, click, type, "
                  "read window contents.",
         "examples": ["Открой Chrome", "Сверни окно", "Найди кнопку Сохранить"]},
        {"id": "web_search", "label": "Web Search",
         "desc": "Web-grounded answers via Claude or Gemini's own search tools.",
         "examples": ["Найди последние новости про...", "Сколько стоит..."]},
        {"id": "memory",     "label": "Memory",
         "desc": "Remembers facts and preferences across sessions — stored locally.",
         "examples": ["Запомни, что...", "Что ты знаешь обо мне?"]},
        {"id": "telegram",   "label": "Telegram",
         "desc": "Second-account userbot — send commands and receive replies remotely.",
         "examples": []},
        {"id": "dashboard",  "label": "Remote",
         "desc": "Phone dashboard over the local network — QR pairing, voice relay.",
         "examples": []},
    ]

    node_clicked = pyqtSignal(str)

    @staticmethod
    def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
        return max(minimum, min(maximum, value))

    @classmethod
    def _ease(cls, value: float) -> float:
        value = cls._clamp(value)
        return value * value * (3.0 - 2.0 * value)

    @classmethod
    def _spring(cls, value: float) -> float:
        """A restrained overshoot so assembled particles settle organically."""
        value = cls._clamp(value)
        return 1.0 + (value - 1.0) * math.exp(-5.2 * value) * math.cos(12.0 * value)

    def __init__(self, face_path: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(300, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.muted    = False
        self.speaking = False
        self.state    = "INITIALISING"

        self._tick   = 0
        self._t0     = time.time()

        self._energy, self._tgt_energy = 0.16, 0.16
        self._warmth, self._tgt_warmth = 0.05, 0.05

        # mouth: eased toward a new random "openness" target on a short
        # timer while speaking (syllable-like jumps), settles to a thin
        # resting line otherwise — same spring-easing idea the old
        # scale/halo animation used, just applied to a more legible signal.
        self._mouth_open, self._tgt_mouth_open = 0.08, 0.08
        self._mouth_retarget_t = 0.0

        self._orbit_angle = 0.0
        self._pulses: list[dict] = []
        self._pulse_timer = 0.0
        self._sparks: list[dict] = []
        self._spark_timer = 0.0

        self._blink = 0.0  # 0 = eyes open, 1 = fully shut
        self._blink_phase = "open"
        self._blink_timer = random.uniform(2.5, 5.0)

        # face assembly: 0 = eyes/mouth dispersed back into the ambient
        # particle field (invisible), 1 = fully formed. Eases toward 1 the
        # instant a response starts speaking, and back toward 0 once it
        # ends — the face visibly materializes to talk rather than sitting
        # there statically the whole time.
        self._face_assembly = 0.0
        self._prev_speaking = False
        self._startup_active = False
        self._startup_progress = 0.0
        self._security_scan_active = False
        self._security_scan_message = ""
        self._scan_theme = 0.0
        self._power_mode = False
        self._power_theme = 0.0

        self._particles: list[dict] = []
        self._face_geom = (0.0, 0.0, 0.0, 0.0)  # cx, cy, rx, ry
        self._geom_wh = (0, 0)

        # Subsystem constellation: real component status (updated from
        # main.py via set_subsystem_status()) and screen positions (computed
        # in _build_particles() alongside everything else that depends on
        # _face_geom). Defaults to "online" so nodes don't flash red before
        # the first real status report arrives shortly after startup.
        self._node_status: dict[str, bool] = {n["id"]: True for n in self._NODES}
        self._node_positions: list[tuple[str, float, float]] = []
        self._node_hover: str | None = None

        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(16)

        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def set_subsystem_status(self, status: dict[str, bool]) -> None:
        """Thread-unsafe by itself — call via MainWindow's _status_sig, same
        pattern as _log_sig/_state_sig/_content_sig, never directly from a
        background thread."""
        self._node_status.update(status)
        self.update()

    def begin_startup_sequence(self) -> None:
        """Reveal the face only after the splash closes, while the HUD is visible."""
        self._startup_progress = 0.0
        self._startup_active = True
        self._pulses.clear()
        self._sparks.clear()

    def set_security_scan(self, active: bool, message: str) -> None:
        self._security_scan_active = active
        self._security_scan_message = message
        self.update()

    def set_power_mode(self, active: bool) -> None:
        self._power_mode = active
        self.update()

    def mousePressEvent(self, event) -> None:
        pos = event.position()
        hit = self._node_at(pos.x(), pos.y())
        if hit is not None:
            self.node_clicked.emit(hit)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        pos = event.position()
        hit = self._node_at(pos.x(), pos.y())
        if hit != self._node_hover:
            self._node_hover = hit
            self.setCursor(Qt.CursorShape.PointingHandCursor if hit else Qt.CursorShape.ArrowCursor)
            self.update()
        super().mouseMoveEvent(event)

    def _node_at(self, x: float, y: float, radius: float = 16.0) -> str | None:
        for node_id, nx, ny in self._node_positions:
            if (nx - x) ** 2 + (ny - y) ** 2 <= radius * radius:
                return node_id
        return None

    # ---- particle field ---------------------------------------------------

    def _build_particles(self) -> None:
        W, H = self.width(), self.height()
        self._geom_wh = (W, H)
        fw = min(W, H)
        cx, cy = W / 2, H * 0.46
        rx, ry = fw * 0.25, fw * 0.29
        self._face_geom = (cx, cy, rx, ry)

        particles: list[dict] = []

        def push(x, y, layer, warm_bias):
            target_angle = math.atan2((y - cy) / max(1.0, ry), (x - cx) / max(1.0, rx))
            if abs(x - cx) < rx * 0.12 and abs(y - cy) < ry * 0.12:
                target_angle = random.uniform(0, math.tau)
            scatter_radius = fw * random.uniform(0.78, 1.22)
            particles.append({
                "bx": x, "by": y,
                "scatter_x": cx + math.cos(target_angle) * scatter_radius,
                "scatter_y": cy + math.sin(target_angle) * scatter_radius,
                "arrival_delay": random.uniform(0.03, 0.40),
                "ang": random.uniform(0, math.tau),
                "spd": random.uniform(0.4, 1.3),
                "r": (1.0 if layer == "core" else 1.5) + random.uniform(0, 1.0),
                "layer": layer,
                "warm_bias": warm_bias,
                "twinkle": random.uniform(0, 20),
                "twinkle_spd": random.uniform(0.6, 2.0),
            })

        # head rim
        RIM_N = 90
        for i in range(RIM_N):
            t = (i / RIM_N) * math.tau
            j = 1 + random.uniform(-0.03, 0.03)
            push(cx + math.cos(t) * rx * j, cy + math.sin(t) * ry * j, "edge", 0.15)

        # volumetric fill — warmer toward the lower-center (jaw) plane,
        # which is what makes the face plate glow warm while speaking
        FILL_N = 150
        for _ in range(FILL_N):
            a = random.uniform(0, math.tau)
            r = math.sqrt(random.random())
            x = cx + math.cos(a) * rx * r * 0.9
            y = cy + math.sin(a) * ry * r * 0.9
            face_bias = max(0.0, 1 - abs(x - cx) / (rx * 0.85) - max(0.0, y - cy) / (ry * 0.7))
            push(x, y, "core", 0.2 + face_bias * 0.9)

        # mouth: two rows of particles (upper/lower "lip") whose vertical
        # gap opens with self._mouth_open at paint time — a real, visible
        # talking cue rather than an abstract glow.
        MOUTH_N = 16
        mcx = cx
        mcy = cy + ry * 0.5
        mhw = rx * 0.34
        for i in range(MOUTH_N):
            mt = (i / (MOUTH_N - 1)) * 2 - 1  # -1..1 across the mouth width
            sa = random.uniform(0, math.tau)
            sr = fw * random.uniform(0.55, 0.95)
            particles.append({
                "mouth_t": mt, "mouth_role": "upper" if i % 2 == 0 else "lower",
                "mcx": mcx, "mcy": mcy, "mhw": mhw,
                # scatter origin: where this particle sits while dispersed
                # (out beyond the head rim) — it flies inward to its mouth
                # position as _face_assembly eases toward 1.
                "scatter_x": cx + math.cos(sa) * sr,
                "scatter_y": cy + math.sin(sa) * sr * (ry / rx),
                "r": 1.1 + random.uniform(0, 0.6),
                "layer": "mouth",
                "twinkle": random.uniform(0, 20),
                "twinkle_spd": random.uniform(0.8, 1.6),
            })

        # eyes: small glowing clusters, blink by scaling toward zero height
        EYE_N = 7
        for side in (-1, 1):
            ecx = cx + side * rx * 0.34
            ecy = cy - ry * 0.10
            for _ in range(EYE_N):
                a = random.uniform(0, math.tau)
                r = math.sqrt(random.random()) * rx * 0.05
                sa = random.uniform(0, math.tau)
                sr = fw * random.uniform(0.55, 0.95)
                particles.append({
                    "eye_bx": ecx + math.cos(a) * r, "eye_by": ecy + math.sin(a) * r,
                    "eye_cy": ecy,
                    "scatter_x": cx + math.cos(sa) * sr,
                    "scatter_y": cy + math.sin(sa) * sr * (ry / rx),
                    "r": 1.0 + random.uniform(0, 0.5),
                    "layer": "eye",
                    "twinkle": random.uniform(0, 20),
                    "twinkle_spd": random.uniform(0.5, 1.2),
                })

        self._particles = particles

        # Subsystem constellation positions — spread around the head,
        # leaving a gap at the bottom where the status text/waveform live.
        # Elliptical radius (scaled by ry/rx on the y-axis) to match the
        # head's own aspect ratio rather than looking like a mismatched circle.
        node_count = len(self._NODES)
        gap_deg = 70.0
        node_radius = min(rx * 2.6, cx - 70, W - cx - 70, cy - 50)
        positions = []
        for i, node in enumerate(self._NODES):
            frac = i / (node_count - 1) if node_count > 1 else 0.0
            deg = 90 + gap_deg / 2 + frac * (360 - gap_deg)
            rad = math.radians(deg)
            nx = cx + math.cos(rad) * node_radius
            ny = cy + math.sin(rad) * node_radius * (ry / rx)
            positions.append((node["id"], nx, ny))
        self._node_positions = positions

    @staticmethod
    def _mix(pri: tuple[int, int, int], acc: tuple[int, int, int], w: float) -> tuple[int, int, int]:
        w = max(0.0, min(1.0, w))
        return (
            int(pri[0] + (acc[0] - pri[0]) * w),
            int(pri[1] + (acc[1] - pri[1]) * w),
            int(pri[2] + (acc[2] - pri[2]) * w),
        )

    def _spawn_spark(self) -> None:
        cx, cy, rx, ry = self._face_geom
        a = random.uniform(0, math.tau)
        r0 = rx * 0.3
        self._sparks.append({
            "x": cx + math.cos(a) * r0, "y": cy + math.sin(a) * r0 * (ry / max(1.0, rx)),
            "vx": math.cos(a) * random.uniform(0.6, 2.0),
            "vy": math.sin(a) * random.uniform(0.6, 2.0),
            "life": 0.0, "max_life": random.uniform(0.5, 1.1),
            "r": 0.8 + random.uniform(0, 1.0),
        })

    def _step(self) -> None:
        self._tick += 1
        dt = 0.016

        if self.muted:
            target = self._MUTED_TARGET
        else:
            target = self._STATE_TARGETS.get(self.state, self._DEFAULT_TARGET)
        self._tgt_energy, self._tgt_warmth = target
        self._energy += (self._tgt_energy - self._energy) * 0.06
        self._warmth += (self._tgt_warmth - self._warmth) * 0.05
        scan_target = 1.0 if self._security_scan_active else 0.0
        self._scan_theme += (scan_target - self._scan_theme) * 0.045
        power_target = 1.0 if self._power_mode else 0.0
        self._power_theme += (power_target - self._power_theme) * 0.055

        if self._startup_active:
            self._startup_progress = min(1.0, self._startup_progress + dt / 2.15)
            if self._startup_progress >= 1.0:
                self._startup_active = False
                self._pulses.append({"life": 0.0, "max_life": 1.5})

        # mouth: retarget on a short, speech-like cadence while speaking;
        # otherwise ease down to a near-closed resting line
        self._mouth_retarget_t -= dt
        if self.speaking and self._mouth_retarget_t <= 0:
            self._tgt_mouth_open = random.uniform(0.15, 1.0)
            self._mouth_retarget_t = random.uniform(0.08, 0.22)
        elif not self.speaking:
            self._tgt_mouth_open = 0.08
        mouth_sp = 0.5 if self.speaking else 0.12
        self._mouth_open += (self._tgt_mouth_open - self._mouth_open) * mouth_sp

        # The face assembles visibly after the splash, then stays quietly
        # present in idle mode. Speaking completes the assembly and gives
        # the eyes/mouth their full intensity.
        idle_assembly = 0.72 if self._startup_progress >= 1.0 else self._startup_progress
        asm_target = 1.0 if self.speaking else idle_assembly
        asm_rate = 0.16 if self.speaking else 0.055
        self._face_assembly += (asm_target - self._face_assembly) * asm_rate
        if self.speaking and not self._prev_speaking:
            # rising edge: response just started — give the materialization
            # an extra beat with an immediate pulse ring and a spark burst
            self._pulses.append({"life": 0.0, "max_life": 1.3})
            for _ in range(10):
                self._spawn_spark()
        self._prev_speaking = self.speaking

        self._orbit_angle += dt * (0.35 + self._energy * 1.6 + self._power_theme * 2.8)

        # pulse rings — spawn faster and reach further as energy climbs
        self._pulse_timer -= dt
        if self._pulse_timer <= 0:
            self._pulses.append({"life": 0.0, "max_life": random.uniform(1.1, 1.6)})
            self._pulse_timer = max(0.18, 1.7 - self._energy * 1.5 - self._warmth * 0.6 - self._power_theme * 0.7)
        for pr in self._pulses:
            pr["life"] += dt
        self._pulses = [pr for pr in self._pulses if pr["life"] < pr["max_life"]]

        # sparks — ejected from the core, density follows warmth
        self._spark_timer -= dt
        if self._spark_timer <= 0 and (self._warmth > 0.15 or self._energy > 0.5):
            self._spawn_spark()
            self._spark_timer = max(0.025, 0.5 - self._warmth * 0.44 - self._energy * 0.06 - self._power_theme * 0.22)
        for sk in self._sparks:
            sk["life"] += dt
            sk["x"] += sk["vx"] * dt * 60 * (0.4 + self._energy)
            sk["y"] += sk["vy"] * dt * 60 * (0.4 + self._energy)
        self._sparks = [sk for sk in self._sparks if sk["life"] < sk["max_life"]]

        # blink
        self._blink_timer -= dt
        if self._blink_phase == "open" and self._blink_timer <= 0:
            self._blink_phase = "closing"
        elif self._blink_phase == "closing":
            self._blink = min(1.0, self._blink + dt * 12)
            if self._blink >= 1.0:
                self._blink_phase = "opening"
        elif self._blink_phase == "opening":
            self._blink = max(0.0, self._blink - dt * 10)
            if self._blink <= 0.0:
                self._blink_phase = "open"
                self._blink_timer = random.uniform(2.5, 6.0)

        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        W, H = self.width(), self.height()
        if (W, H) != self._geom_wh:
            self._build_particles()
        cx, cy, rx, ry = self._face_geom
        fw = min(W, H)
        t = time.time() - self._t0
        energy, warmth = self._energy, self._warmth
        startup_raw = self._clamp((self._startup_progress - 0.04) / 0.72)
        startup = self._ease(startup_raw)

        PRI = self._mix((0x7F, 0xE3, 0xFF), (0x45, 0xFF, 0x9A), self._scan_theme)
        ACC = self._mix((0xFF, 0x9D, 0x6B), (0x00, 0xC8, 0x6A), self._scan_theme)
        PRI = self._mix(PRI, (0xFF, 0xB3, 0x38), self._power_theme)
        ACC = self._mix(ACC, (0xFF, 0x5B, 0x36), self._power_theme)

        # Layered mica-like backdrop for a restrained Windows 11 dark surface.
        bg = QRadialGradient(cx, cy * 0.9, fw * 1.15)
        base_core = self._mix((0x16, 0x21, 0x2D), (0x08, 0x2A, 0x18), self._scan_theme)
        base_mid = self._mix((0x0D, 0x15, 0x1E), (0x05, 0x17, 0x0D), self._scan_theme)
        bg.setColorAt(0.0, rgbcol(self._mix(base_core, (0x38, 0x0C, 0x0E), self._power_theme)))
        bg.setColorAt(0.38, rgbcol(self._mix(base_mid, (0x18, 0x05, 0x08), self._power_theme)))
        bg.setColorAt(0.7, qcol(C.BG))
        bg.setColorAt(1.0, qcol(C.BG))
        p.fillRect(self.rect(), bg)

        # Power Mode gets a moving alert field behind the face.
        if self._power_theme > 0.02:
            stripe_alpha = int(42 * self._power_theme)
            p.setPen(QPen(rgbcol((0xFF, 0x42, 0x36), stripe_alpha), 1.2))
            offset = (t * 95) % 48
            for x in range(-H, W + H, 48):
                p.drawLine(QPointF(x + offset, 0), QPointF(x + H + offset, H))

        # Fine grid grounds the animation without competing with the face.
        grid_alpha = int((13 + energy * 13 + self._power_theme * 22) * (0.15 + startup * 0.85))
        grid_color = self._mix((0x16, 0x35, 0x41), (0x78, 0x16, 0x18), self._power_theme)
        p.setPen(QPen(rgbcol(grid_color, grid_alpha), 1))
        grid_step = max(36, int(fw / 12))
        for x in range(0, W, grid_step):
            p.drawLine(x, 0, x, H)
        for y in range(0, H, grid_step):
            p.drawLine(0, y, W, y)

        # ambient halo behind the whole face, brightens with warmth/energy
        accent = C.MUTED_C if self.muted else PRI
        halo_a = int(max(0, min(255, (60 + energy * 90 + warmth * 60) * startup)))
        halo = QRadialGradient(cx, cy, rx * 2.4)
        accent_col = qcol(accent, int(halo_a * 0.5)) if isinstance(accent, str) else rgbcol(accent, int(halo_a * 0.5))
        accent_dim = qcol(accent, int(halo_a * 0.14)) if isinstance(accent, str) else rgbcol(accent, int(halo_a * 0.14))
        accent_clear = qcol(accent, 0) if isinstance(accent, str) else rgbcol(accent, 0)
        halo.setColorAt(0.0, accent_col)
        halo.setColorAt(0.45, accent_dim)
        halo.setColorAt(1.0, accent_clear)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(halo)
        p.drawEllipse(QRectF(cx - rx * 2.4, cy - ry * 2.4, rx * 4.8, ry * 4.8))

        # pulse rings — expanding energy waves timed to speaking intensity
        for pr in self._pulses:
            pf = pr["life"] / pr["max_life"]
            prad = rx * (0.95 + pf * (2.4 + energy * 1.6))
            palpha = (1 - pf) * (0.16 + warmth * 0.18 + energy * 0.06)
            pc = self._mix(PRI, ACC, warmth)
            p.setPen(QPen(rgbcol(pc, int(255 * palpha * (1 + self._power_theme * 0.7))), 1.1 + self._power_theme * 1.1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - prad, cy - prad * (ry / rx), prad * 2, prad * 2 * (ry / rx)))

        # orbiting scan ring — segmented, speeds up with energy
        ORBIT_SEGMENTS = 20
        orbit_r, orbit_ry = rx * 1.45, ry * 1.32
        oc = self._mix(PRI, ACC, warmth * 0.7)
        for s in range(ORBIT_SEGMENTS):
            seg_len = (math.tau / ORBIT_SEGMENTS) * 0.55
            a0 = self._orbit_angle + (s / ORBIT_SEGMENTS) * math.tau
            seg_alpha = startup * (0.12 + energy * 0.22) * (0.4 + 0.6 * abs(math.sin(s * 1.7 + self._orbit_angle * 1.3)))
            path = QPainterPath()
            path.arcMoveTo(QRectF(cx - orbit_r, cy - orbit_ry, orbit_r * 2, orbit_ry * 2), math.degrees(-a0))
            path.arcTo(QRectF(cx - orbit_r, cy - orbit_ry, orbit_r * 2, orbit_ry * 2), math.degrees(-a0), -math.degrees(seg_len))
            p.setPen(QPen(rgbcol(oc, int(255 * seg_alpha)), 1.3))
            p.drawPath(path)

        if self._power_theme > 0.02:
            for ring_scale, speed, alpha in ((1.78, -1.6, 0.46), (2.06, 2.45, 0.25)):
                ring_r, ring_ry = rx * ring_scale, ry * ring_scale
                phase = self._orbit_angle * speed
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(rgbcol((0xFF, 0x5B, 0x36), int(255 * alpha * self._power_theme)), 1.0))
                for segment in range(6):
                    angle = math.degrees(phase + segment * math.tau / 6)
                    p.drawArc(QRectF(cx - ring_r, cy - ring_ry, ring_r * 2, ring_ry * 2), int(-angle * 16), int(-28 * 16))

        # subsystem constellation — real JARVIS components orbiting the
        # core; a filament connects each to the orbit ring, an outlined dot
        # marks its position (lit if online, dim/dashed if not), and its
        # label sits just outside. Click to see what it does (paintEvent
        # only draws; hit-testing is in mousePressEvent, sharing the exact
        # same _node_positions this loop computes screen coords from).
        p.setFont(QFont(_fonts.UI_FONT, 9))
        for node_id, nx, ny in self._node_positions:
            online = self._node_status.get(node_id, True)
            hovered = self._node_hover == node_id
            node_color = self._mix(PRI, ACC, warmth * 0.5) if online else (0x55, 0x62, 0x70)
            line_alpha = startup * ((0.16 if online else 0.08) + (0.12 if hovered else 0.0))
            p.setPen(QPen(rgbcol(node_color, int(255 * line_alpha)), 1.0))
            edge_t = math.atan2(ny - cy, nx - cx)
            edge_x = cx + math.cos(edge_t) * rx * 1.45
            edge_y = cy + math.sin(edge_t) * ry * 1.32
            p.drawLine(QPointF(edge_x, edge_y), QPointF(nx, ny))

            dot_r = (5.5 if hovered else 4.0) if online else 3.2
            p.setPen(Qt.PenStyle.NoPen if online else QPen(rgbcol(node_color, 200), 1.2))
            p.setBrush(QBrush(rgbcol(node_color, int((235 if online else 40) * startup))))
            p.drawEllipse(QPointF(nx, ny), dot_r, dot_r)
            if hovered:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(rgbcol(node_color, 60)))
                p.drawEllipse(QPointF(nx, ny), dot_r + 5, dot_r + 5)

            label = next((n["label"] for n in self._NODES if n["id"] == node_id), node_id)
            label_x = nx + (14 if nx >= cx else -14)
            align = Qt.AlignmentFlag.AlignLeft if nx >= cx else Qt.AlignmentFlag.AlignRight
            text_w = 130
            box_x = label_x if nx >= cx else label_x - text_w
            text_color = qcol(C.TEXT_MED, int((235 if (online or hovered) else 120) * startup))
            p.setPen(QPen(text_color, 1))
            p.drawText(QRectF(box_x, ny - 8, text_w, 16), align | Qt.AlignmentFlag.AlignVCenter, label)

        # connective lines between nearby head particles — sparse, faint
        head_pts = [pt for pt in self._particles if pt.get("layer") in ("edge", "core")]
        col = self._mix(PRI, ACC, warmth)
        thresh2 = (fw * 0.045) ** 2
        for i in range(0, len(head_pts), 5):
            hp = head_pts[i]
            for hq in head_pts[i + 1:i + 26]:
                dx, dy = hp["bx"] - hq["bx"], hp["by"] - hq["by"]
                d2 = dx * dx + dy * dy
                if d2 < thresh2:
                    a = startup * (1 - d2 / thresh2) * 0.10 * (0.5 + energy)
                    p.setPen(QPen(rgbcol(col, int(255 * a)), 0.6))
                    p.drawLine(QPointF(hp["bx"], hp["by"]), QPointF(hq["bx"], hq["by"]))

        # head particles — drift + twinkle
        amp = 2.2 + energy * 7.5 + self._power_theme * 6.5
        for pt in head_pts:
            drift_t = t * pt["spd"]
            dx = math.cos(drift_t + pt["ang"]) * amp * (0.7 if pt["layer"] == "core" else 1.0)
            dy = math.sin(drift_t * 1.3 + pt["ang"]) * amp * (0.55 if pt["layer"] == "core" else 0.9)
            arrival_raw = self._clamp((startup - pt["arrival_delay"]) / 0.52)
            arrival = self._spring(arrival_raw)
            tx, ty = pt["bx"] + dx, pt["by"] + dy
            x = pt["scatter_x"] + (tx - pt["scatter_x"]) * arrival
            y = pt["scatter_y"] + (ty - pt["scatter_y"]) * arrival

            w = min(1.0, warmth + pt["warm_bias"] * warmth * 1.4)
            c = self._mix(PRI, ACC, w)
            base_alpha = 0.85 if pt["layer"] == "core" else 0.75
            pulse = 0.75 + math.sin(drift_t * 2 + pt["ang"]) * 0.25 * (0.4 + energy)
            twinkle_wave = math.sin(t * pt["twinkle_spd"] + pt["twinkle"])
            twinkle_boost = max(0.0, (twinkle_wave - 0.92) / 0.08)
            visible_arrival = self._clamp(arrival)
            alpha = visible_arrival * max(0.03, min(1.0, base_alpha * pulse * (0.55 + energy * 0.6) + twinkle_boost * 0.5))
            r = pt["r"] * visible_arrival * (0.85 + energy * 0.5) * (1 + twinkle_boost * 0.9)

            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(rgbcol(c, int(255 * alpha))))
            p.drawEllipse(QPointF(x, y), r, r)

        # face assembly ease — smoothstep gives a decelerating "arrival"
        # rather than a linear fly-in, so particles settle instead of snap.
        asm = self._face_assembly
        asm_e = asm * asm * (3 - 2 * asm)

        # mouth — a warm glow that widens/opens with self._mouth_open,
        # framed by upper/lower "lip" particle rows. This is the actual
        # talking cue: dispersed at rest, assembles from the particle field
        # and becomes active the moment a response starts speaking.
        mouth_pts = [pt for pt in self._particles if pt.get("layer") == "mouth"]
        if mouth_pts and asm_e > 0.003:
            mcx, mcy, mhw = mouth_pts[0]["mcx"], mouth_pts[0]["mcy"], mouth_pts[0]["mhw"]
            gap = ry * 0.16 * self._mouth_open
            mc = self._mix(PRI, ACC, max(0.35, warmth))
            mglow = QRadialGradient(mcx, mcy, mhw * 1.3)
            mglow.setColorAt(0.0, rgbcol(mc, int((70 + 120 * self._mouth_open) * asm_e)))
            mglow.setColorAt(1.0, rgbcol(mc, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(mglow)
            p.drawEllipse(QPointF(mcx, mcy), mhw * 1.15, ry * 0.16 + gap * 1.4)

            for pt in mouth_pts:
                mt = pt["mouth_t"]
                tx = mcx + mt * mhw
                curve = (1 - mt * mt) * 0.6
                offset = gap * (0.35 + curve)
                ty = mcy - offset if pt["mouth_role"] == "upper" else mcy + offset
                x = pt["scatter_x"] + (tx - pt["scatter_x"]) * asm_e
                y = pt["scatter_y"] + (ty - pt["scatter_y"]) * asm_e
                twinkle_wave = math.sin(t * pt["twinkle_spd"] + pt["twinkle"])
                alpha = (0.7 + max(0.0, twinkle_wave) * 0.3) * asm_e
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(rgbcol(mc, int(255 * alpha))))
                p.drawEllipse(QPointF(x, y), pt["r"], pt["r"])

        # eyes — small glowing clusters that blink; assemble in from the
        # particle field the same way the mouth does.
        eye_pts = [pt for pt in self._particles if pt.get("layer") == "eye"]
        if asm_e > 0.003:
            eye_scale = 1.0 - self._blink
            ec = self._mix(PRI, ACC, warmth * 0.5)
            for pt in eye_pts:
                tx = pt["eye_bx"]
                ty = pt["eye_cy"] + (pt["eye_by"] - pt["eye_cy"]) * eye_scale
                x = pt["scatter_x"] + (tx - pt["scatter_x"]) * asm_e
                y = pt["scatter_y"] + (ty - pt["scatter_y"]) * asm_e
                twinkle_wave = math.sin(t * pt["twinkle_spd"] + pt["twinkle"])
                alpha = (0.55 + max(0.0, twinkle_wave) * 0.35) * (0.2 + eye_scale * 0.8) * asm_e
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(rgbcol(ec, int(255 * max(0.0, alpha)))))
                p.drawEllipse(QPointF(x, y), pt["r"] * (0.6 + eye_scale * 0.5), pt["r"] * (0.6 + eye_scale * 0.5))

        # Startup scan: a moving visor locks onto the forming head. Its main
        # line, vertical acquisition beam, and scan-zone glow fade away once
        # initialization hands off to the live HUD.
        if self._startup_active:
            scan_progress = self._ease(self._clamp((self._startup_progress - 0.10) / 0.78))
            scan_y = cy - ry * 1.16 + scan_progress * ry * 2.32
            scan = QLinearGradient(0, scan_y - 24, 0, scan_y + 24)
            scan.setColorAt(0.0, qcol(C.PRI, 0))
            scan.setColorAt(0.42, qcol(C.PRI, 0))
            scan.setColorAt(0.49, qcol(C.PRI, 105))
            scan.setColorAt(0.5, qcol(C.WHITE, 230))
            scan.setColorAt(0.51, qcol(C.PRI, 105))
            scan.setColorAt(0.58, qcol(C.PRI, 0))
            scan.setColorAt(1.0, qcol(C.PRI, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(scan)
            p.drawRect(QRectF(cx - rx * 1.28, scan_y - 24, rx * 2.56, 48))

            visor_x = cx + math.sin(self._startup_progress * math.tau * 4.6) * rx * 0.62
            p.setPen(QPen(qcol(C.PRI, 110), 1))
            p.drawLine(QPointF(visor_x, cy - ry * 1.10), QPointF(visor_x, cy + ry * 1.10))
            p.setPen(QPen(qcol(C.WHITE, 190), 1.5))
            p.drawLine(QPointF(cx - rx * 1.12, scan_y), QPointF(cx + rx * 1.12, scan_y))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(qcol(C.PRI, 150), 1.2))
            scan_r = rx * (0.18 + self._startup_progress * 0.78)
            p.drawEllipse(QPointF(cx, cy), scan_r, scan_r * (ry / rx))

        # sparks — ejected from the core, density follows warmth
        for sk in self._sparks:
            sf = sk["life"] / sk["max_life"]
            salpha = (1 - sf) * (0.5 + warmth * 0.4)
            sc = self._mix(PRI, ACC, min(1.0, warmth * 1.2 + 0.2))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(rgbcol(sc, int(255 * max(0.0, salpha)))))
            sr = sk["r"] * (1 - sf * 0.4)
            p.drawEllipse(QPointF(sk["x"], sk["y"]), sr, sr)

        if self._security_scan_active:
            scan_phase = 0.5 - 0.5 * math.cos(t * math.tau * 0.72)
            scan_y = cy - ry * 0.88 + scan_phase * ry * 1.76
            glow = QLinearGradient(0, scan_y - 28, 0, scan_y + 28)
            glow.setColorAt(0.0, qcol(C.GREEN, 0))
            glow.setColorAt(0.48, qcol(C.GREEN, 28))
            glow.setColorAt(0.50, qcol(C.GREEN, 205))
            glow.setColorAt(0.52, qcol(C.GREEN, 28))
            glow.setColorAt(1.0, qcol(C.GREEN, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(glow)
            p.drawRect(QRectF(cx - rx * 1.12, scan_y - 28, rx * 2.24, 56))
            p.setPen(QPen(qcol(C.GREEN, 215), 1.4))
            p.drawLine(QPointF(cx - rx * 1.08, scan_y), QPointF(cx + rx * 1.08, scan_y))
            tracker_x = cx + math.sin(t * 3.7) * rx * 0.72
            tracker_y = scan_y + math.sin(t * 7.4) * 5
            p.setPen(QPen(qcol(C.GREEN, 140), 1))
            p.drawLine(QPointF(tracker_x, cy - ry * 0.95), QPointF(tracker_x, cy + ry * 0.95))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(qcol(C.GREEN, int(90 + 80 * (0.5 + 0.5 * math.sin(t * 5.0)))), 1.0))
            tracker_r = rx * (0.13 + 0.04 * math.sin(t * 5.0))
            p.drawEllipse(QPointF(tracker_x, tracker_y), tracker_r, tracker_r * (ry / rx))
            p.setFont(QFont(_fonts.MONO_FONT, 8, QFont.Weight.Bold))
            p.setPen(QPen(qcol(C.GREEN, 235), 1))
            p.drawText(QRectF(0, cy - ry * 1.35, W, 18), Qt.AlignmentFlag.AlignCenter, "MICROSOFT DEFENDER // SCANNING")

        if self._power_theme > 0.02:
            p.setFont(QFont(_fonts.MONO_FONT, 8, QFont.Weight.Bold))
            p.setPen(QPen(rgbcol((0xFF, 0xB3, 0x38), int(235 * self._power_theme)), 1))
            p.drawText(QRectF(0, cy - ry * 1.35, W, 18), Qt.AlignmentFlag.AlignCenter, "POWER MODE // ENHANCED")
            p.setPen(QPen(rgbcol((0xFF, 0x5B, 0x36), int(180 * self._power_theme)), 1))
            p.drawText(QRectF(0, cy + ry * 1.12, W, 16), Qt.AlignmentFlag.AlignCenter, "CORE OUTPUT // MAXIMUM")

        # status text
        sy = cy + fw * 0.36
        if self.muted:
            txt, col = "Muted", qcol(C.MUTED_C)
        elif self.speaking:
            txt, col = "Speaking", qcol(C.WHITE)
        elif self.state == "THINKING":
            txt, col = "Thinking", qcol(C.ACC2)
        elif self.state == "PROCESSING":
            txt, col = "Processing", qcol(C.ACC2)
        elif self.state == "LISTENING":
            txt, col = "Listening", qcol(C.WHITE)
        else:
            txt, col = self.state.capitalize(), qcol(C.PRI)

        p.setPen(QPen(col, 1))
        p.setFont(QFont(_fonts.UI_FONT, 15, QFont.Weight.Bold))
        p.drawText(QRectF(0, sy, W, 26), Qt.AlignmentFlag.AlignCenter, txt)

        sub_y = sy + 27
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.setFont(QFont(_fonts.UI_FONT, 9))
        subtitle = "Initializing neural interface" if self._startup_active else 'Say "Jarvis" or press the mic'
        p.drawText(QRectF(0, sub_y, W, 18), Qt.AlignmentFlag.AlignCenter, subtitle)

        # waveform
        wy = sub_y + 24
        N, bw = 28, 7
        wx0 = (W - N * bw) / 2
        for i in range(N):
            if self.muted:
                hgt, cl = 2, qcol(C.MUTED_C, 150)
            elif self.speaking:
                hgt = random.randint(3, int(16 + self._power_theme * 16))
                cl  = rgbcol(PRI, 220) if hgt > 10 else rgbcol(ACC, 150)
            else:
                hgt = int(2 + 2 * math.sin(self._tick * 0.07 + i * 0.6))
                cl  = qcol(C.BORDER_B, 170)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(cl))
            p.drawRoundedRect(QRectF(wx0 + i * bw, wy + 16 - hgt, bw - 2, max(2, hgt)), 2, 2)
