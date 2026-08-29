from __future__ import annotations

import json
import math
import os
import platform
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

if platform.system() == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}

from PyQt6.QtCore import (
    QEasingCurve, QMimeData, QObject, QPointF, QRectF, QSize, Qt,
    QTimer, QUrl, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QDragEnterEvent, QDropEvent, QFont, QFontDatabase,
    QKeySequence, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
    QRadialGradient, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget, QProgressBar,
)

from core.path_utils import resource_path, get_config_path

BASE_DIR   = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
API_FILE   = get_config_path()

_DEFAULT_W, _DEFAULT_H = 980, 700
_MIN_W,     _MIN_H     = 820, 580
_LEFT_W  = 148
_RIGHT_W = 340

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"


class C:
    """Minimal Glass palette — one soft accent, calm neutrals, semantic
    warning/critical colors kept separate from the accent hue."""
    BG        = "#080b10"
    PANEL     = "#0d1218"
    PANEL2    = "#10161d"
    BORDER    = "#1c232c"
    BORDER_B  = "#2a3540"
    BORDER_A  = "#212a33"
    PRI       = "#7fe3ff"
    PRI_DIM   = "#4a8fa3"
    PRI_GHO   = "#132630"
    ACC       = "#ff9d6b"     # warning
    ACC2      = "#cfe3ea"     # neutral highlight (was amber)
    GREEN     = "#9fe8c9"
    GREEN_D   = "#5fae8c"
    RED       = "#ff9d9d"     # critical
    MUTED_C   = "#ff9d9d"
    TEXT      = "#cfe3ea"
    TEXT_DIM  = "#5c6b78"
    TEXT_MED  = "#8a97a3"
    WHITE     = "#eef6f9"
    DARK      = "#0a0e14"
    BAR_BG    = "#141a21"


def qcol(h: str, a: int = 255) -> QColor:
    c = QColor(h); c.setAlpha(a); return c


def rgbcol(rgb: tuple[int, int, int], a: int = 255) -> QColor:
    """Direct-int QColor construction — avoids the hex-string round trip
    qcol() does, which matters in tight per-particle animation loops."""
    return QColor(rgb[0], rgb[1], rgb[2], max(0, min(255, a)))


# ── Bundled typography ────────────────────────────────────────────────────
# Sora (UI labels, headings, prose) + IBM Plex Mono (data: clocks, metrics,
# codes, log timestamps). Loaded from config/fonts/ once a QApplication
# exists (see _load_bundled_fonts, called from JarvisUI.__init__); these
# module-level names are updated in place, with system-font fallbacks if
# the bundled files can't be loaded.
UI_FONT   = "Segoe UI" if _OS == "Windows" else ("SF Pro Text" if _OS == "Darwin" else "Noto Sans")
MONO_FONT = "Consolas" if _OS == "Windows" else ("Menlo" if _OS == "Darwin" else "monospace")


def _load_bundled_fonts() -> None:
    """Register the bundled Sora / IBM Plex Mono font files. Must run after
    a QApplication exists. Falls back to system fonts silently on failure."""
    global UI_FONT, MONO_FONT
    fonts_dir = resource_path("config/fonts")
    try:
        ui_id = QFontDatabase.addApplicationFont(str(fonts_dir / "Sora-Variable.ttf"))
        if ui_id != -1 and QFontDatabase.applicationFontFamilies(ui_id):
            UI_FONT = "Sora"
        for fname in ("IBMPlexMono-Regular.ttf", "IBMPlexMono-Medium.ttf", "IBMPlexMono-Bold.ttf"):
            mono_id = QFontDatabase.addApplicationFont(str(fonts_dir / fname))
            if mono_id != -1 and QFontDatabase.applicationFontFamilies(mono_id):
                MONO_FONT = "IBM Plex Mono"
    except Exception as e:
        print(f"[Fonts] Bundled font load failed, using system fonts: {e}")


# ── Windows GPU via NVML DLL (no subprocess, no console window) ──────────────
_nvml_lib: object = None   # cached ctypes DLL
_nvml_ok:  object = None   # None=untested, True=works, False=unavailable


def _nvml_gpu_windows() -> float:
    """Return NVIDIA GPU utilisation % using nvml.dll directly — zero subprocess."""
    global _nvml_lib, _nvml_ok
    if _nvml_ok is False:
        return -1.0
    try:
        import ctypes

        class _Util(ctypes.Structure):
            _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

        if _nvml_lib is None:
            for dll_name in ("nvml", r"C:\Windows\System32\nvml.dll"):
                try:
                    lib = ctypes.WinDLL(dll_name)
                    lib.nvmlInit_v2()
                    _nvml_lib = lib
                    break
                except Exception:
                    continue

        if _nvml_lib is None:
            import pynvml  # type: ignore
            pynvml.nvmlInit()
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            _nvml_ok = True
            return float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)

        dev = ctypes.c_void_p()
        _nvml_lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
        util = _Util()
        _nvml_lib.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(util))
        _nvml_ok = True
        return float(util.gpu)
    except Exception:
        _nvml_ok = False
        return -1.0


class _SysMetrics:
    def __init__(self):
        self.cpu  = 0.0
        self.mem  = 0.0
        self.net  = 0.0   
        self.gpu  = -1.0  
        self.tmp  = -1.0  
        self._lock = threading.Lock()
        self._last_net = psutil.net_io_counters()
        self._last_net_t = time.time()
        self._running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _loop(self):
        while self._running:
            try:
                self._update()
            except Exception:
                pass
            time.sleep(1.5)

    def _update(self):
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent

        nc  = psutil.net_io_counters()
        now = time.time()
        dt  = now - self._last_net_t
        if dt > 0:
            sent = (nc.bytes_sent - self._last_net.bytes_sent) / dt
            recv = (nc.bytes_recv - self._last_net.bytes_recv) / dt
            net  = (sent + recv) / (1024 * 1024)
        else:
            net = 0.0
        self._last_net   = nc
        self._last_net_t = now

        gpu = self._get_gpu()

        tmp = self._get_temp()

        with self._lock:
            self.cpu = cpu
            self.mem = mem
            self.net = net
            self.gpu = gpu
            self.tmp = tmp

    def _get_gpu(self) -> float:
        # pynvml — subprocess-free, works on all platforms if installed
        try:
            import pynvml  # type: ignore
            pynvml.nvmlInit()
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            return float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)
        except Exception:
            pass

        # Windows: nvml.dll via ctypes (already cached in _nvml_gpu_windows)
        if _OS == "Windows":
            return _nvml_gpu_windows()

        # Linux / macOS: libnvidia-ml shared lib via ctypes
        try:
            import ctypes
            _lib = "libnvidia-ml.so.1" if _OS == "Linux" else "libnvidia-ml.dylib"

            class _Util(ctypes.Structure):
                _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

            nv = ctypes.CDLL(_lib)
            nv.nvmlInit_v2()
            dev = ctypes.c_void_p()
            nv.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
            u = _Util()
            nv.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(u))
            return float(u.gpu)
        except Exception:
            pass

        return -1.0   # N/A — zero subprocess on all platforms

    def _get_temp(self) -> float:
        # psutil — works on Linux; occasionally Windows with driver support
        try:
            temps = psutil.sensors_temperatures()
            for name in ["coretemp", "k10temp", "cpu_thermal", "acpitz",
                         "cpu-thermal", "zenpower", "it8688"]:
                if name in temps and temps[name]:
                    return temps[name][0].current
            for entries in temps.values():
                if entries:
                    return entries[0].current
        except Exception:
            pass

        # Windows: wmi module (pure Python COM, zero subprocess)
        if _OS == "Windows":
            try:
                import wmi  # type: ignore
                w = wmi.WMI(namespace="root/wmi")
                tz = w.MSAcpi_ThermalZoneTemperature()
                if tz:
                    return (tz[0].CurrentTemperature / 10.0) - 273.15
            except Exception:
                pass

        return -1.0   # N/A — zero subprocess on all platforms

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "cpu": self.cpu,
                "mem": self.mem,
                "net": self.net,
                "gpu": self.gpu,
                "tmp": self.tmp,
            }


_metrics = _SysMetrics()

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
            particles.append({
                "bx": x, "by": y,
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

        # face assembly — eyes/mouth fly in from the ambient particle field
        # the instant a response starts speaking (fast ease-in), and settle
        # back out once it ends (slower ease-out, less abrupt).
        asm_target = 1.0 if self.speaking else 0.0
        asm_rate = 0.16 if self.speaking else 0.045
        self._face_assembly += (asm_target - self._face_assembly) * asm_rate
        if self.speaking and not self._prev_speaking:
            # rising edge: response just started — give the materialization
            # an extra beat with an immediate pulse ring and a spark burst
            self._pulses.append({"life": 0.0, "max_life": 1.3})
            for _ in range(10):
                self._spawn_spark()
        self._prev_speaking = self.speaking

        self._orbit_angle += dt * (0.35 + self._energy * 1.6)

        # pulse rings — spawn faster and reach further as energy climbs
        self._pulse_timer -= dt
        if self._pulse_timer <= 0:
            self._pulses.append({"life": 0.0, "max_life": random.uniform(1.1, 1.6)})
            self._pulse_timer = max(0.35, 1.7 - self._energy * 1.5 - self._warmth * 0.6)
        for pr in self._pulses:
            pr["life"] += dt
        self._pulses = [pr for pr in self._pulses if pr["life"] < pr["max_life"]]

        # sparks — ejected from the core, density follows warmth
        self._spark_timer -= dt
        if self._spark_timer <= 0 and (self._warmth > 0.15 or self._energy > 0.5):
            self._spawn_spark()
            self._spark_timer = max(0.03, 0.5 - self._warmth * 0.44 - self._energy * 0.06)
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

        PRI = (0x7F, 0xE3, 0xFF)
        ACC = (0xFF, 0x9D, 0x6B)

        # soft radial backdrop — a touch of depth, never a hard edge
        bg = QRadialGradient(cx, cy * 0.9, fw * 1.15)
        bg.setColorAt(0.0, qcol("#101822"))
        bg.setColorAt(0.7, qcol(C.BG))
        bg.setColorAt(1.0, qcol(C.BG))
        p.fillRect(self.rect(), bg)

        # ambient halo behind the whole face, brightens with warmth/energy
        accent = C.MUTED_C if self.muted else C.PRI
        halo_a = int(max(0, min(255, 60 + energy * 90 + warmth * 60)))
        halo = QRadialGradient(cx, cy, rx * 2.4)
        halo.setColorAt(0.0,  qcol(accent, int(halo_a * 0.5)))
        halo.setColorAt(0.45, qcol(accent, int(halo_a * 0.14)))
        halo.setColorAt(1.0,  qcol(accent, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(halo)
        p.drawEllipse(QRectF(cx - rx * 2.4, cy - ry * 2.4, rx * 4.8, ry * 4.8))

        # pulse rings — expanding energy waves timed to speaking intensity
        for pr in self._pulses:
            pf = pr["life"] / pr["max_life"]
            prad = rx * (0.95 + pf * (2.4 + energy * 1.6))
            palpha = (1 - pf) * (0.16 + warmth * 0.18 + energy * 0.06)
            pc = self._mix(PRI, ACC, warmth)
            p.setPen(QPen(rgbcol(pc, int(255 * palpha)), 1.1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - prad, cy - prad * (ry / rx), prad * 2, prad * 2 * (ry / rx)))

        # orbiting scan ring — segmented, speeds up with energy
        ORBIT_SEGMENTS = 20
        orbit_r, orbit_ry = rx * 1.45, ry * 1.32
        oc = self._mix(PRI, ACC, warmth * 0.7)
        for s in range(ORBIT_SEGMENTS):
            seg_len = (math.tau / ORBIT_SEGMENTS) * 0.55
            a0 = self._orbit_angle + (s / ORBIT_SEGMENTS) * math.tau
            seg_alpha = (0.12 + energy * 0.22) * (0.4 + 0.6 * abs(math.sin(s * 1.7 + self._orbit_angle * 1.3)))
            path = QPainterPath()
            path.arcMoveTo(QRectF(cx - orbit_r, cy - orbit_ry, orbit_r * 2, orbit_ry * 2), math.degrees(-a0))
            path.arcTo(QRectF(cx - orbit_r, cy - orbit_ry, orbit_r * 2, orbit_ry * 2), math.degrees(-a0), -math.degrees(seg_len))
            p.setPen(QPen(rgbcol(oc, int(255 * seg_alpha)), 1.3))
            p.drawPath(path)

        # subsystem constellation — real JARVIS components orbiting the
        # core; a filament connects each to the orbit ring, an outlined dot
        # marks its position (lit if online, dim/dashed if not), and its
        # label sits just outside. Click to see what it does (paintEvent
        # only draws; hit-testing is in mousePressEvent, sharing the exact
        # same _node_positions this loop computes screen coords from).
        p.setFont(QFont(UI_FONT, 9))
        for node_id, nx, ny in self._node_positions:
            online = self._node_status.get(node_id, True)
            hovered = self._node_hover == node_id
            node_color = self._mix(PRI, ACC, warmth * 0.5) if online else (0x55, 0x62, 0x70)
            line_alpha = (0.16 if online else 0.08) + (0.12 if hovered else 0.0)
            p.setPen(QPen(rgbcol(node_color, int(255 * line_alpha)), 1.0))
            edge_t = math.atan2(ny - cy, nx - cx)
            edge_x = cx + math.cos(edge_t) * rx * 1.45
            edge_y = cy + math.sin(edge_t) * ry * 1.32
            p.drawLine(QPointF(edge_x, edge_y), QPointF(nx, ny))

            dot_r = (5.5 if hovered else 4.0) if online else 3.2
            p.setPen(Qt.PenStyle.NoPen if online else QPen(rgbcol(node_color, 200), 1.2))
            p.setBrush(QBrush(rgbcol(node_color, 235 if online else 40)))
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
            text_color = qcol(C.TEXT_MED, 235 if (online or hovered) else 120)
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
                    a = (1 - d2 / thresh2) * 0.10 * (0.5 + energy)
                    p.setPen(QPen(rgbcol(col, int(255 * a)), 0.6))
                    p.drawLine(QPointF(hp["bx"], hp["by"]), QPointF(hq["bx"], hq["by"]))

        # head particles — drift + twinkle
        amp = 2.2 + energy * 7.5
        for pt in head_pts:
            drift_t = t * pt["spd"]
            dx = math.cos(drift_t + pt["ang"]) * amp * (0.7 if pt["layer"] == "core" else 1.0)
            dy = math.sin(drift_t * 1.3 + pt["ang"]) * amp * (0.55 if pt["layer"] == "core" else 0.9)
            x, y = pt["bx"] + dx, pt["by"] + dy

            w = min(1.0, warmth + pt["warm_bias"] * warmth * 1.4)
            c = self._mix(PRI, ACC, w)
            base_alpha = 0.85 if pt["layer"] == "core" else 0.75
            pulse = 0.75 + math.sin(drift_t * 2 + pt["ang"]) * 0.25 * (0.4 + energy)
            twinkle_wave = math.sin(t * pt["twinkle_spd"] + pt["twinkle"])
            twinkle_boost = max(0.0, (twinkle_wave - 0.92) / 0.08)
            alpha = max(0.03, min(1.0, base_alpha * pulse * (0.55 + energy * 0.6) + twinkle_boost * 0.5))
            r = pt["r"] * (0.85 + energy * 0.5) * (1 + twinkle_boost * 0.9)

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

        # sparks — ejected from the core, density follows warmth
        for sk in self._sparks:
            sf = sk["life"] / sk["max_life"]
            salpha = (1 - sf) * (0.5 + warmth * 0.4)
            sc = self._mix(PRI, ACC, min(1.0, warmth * 1.2 + 0.2))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(rgbcol(sc, int(255 * max(0.0, salpha)))))
            sr = sk["r"] * (1 - sf * 0.4)
            p.drawEllipse(QPointF(sk["x"], sk["y"]), sr, sr)

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
        p.setFont(QFont(UI_FONT, 15, QFont.Weight.Bold))
        p.drawText(QRectF(0, sy, W, 26), Qt.AlignmentFlag.AlignCenter, txt)

        sub_y = sy + 27
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.setFont(QFont(UI_FONT, 9))
        p.drawText(QRectF(0, sub_y, W, 18), Qt.AlignmentFlag.AlignCenter,
                   'say "Jarvis" or press the mic')

        # waveform
        wy = sub_y + 24
        N, bw = 28, 7
        wx0 = (W - N * bw) / 2
        for i in range(N):
            if self.muted:
                hgt, cl = 2, qcol(C.MUTED_C, 150)
            elif self.speaking:
                hgt = random.randint(3, 16)
                cl  = qcol(C.PRI, 210) if hgt > 10 else qcol(C.PRI, 120)
            else:
                hgt = int(2 + 2 * math.sin(self._tick * 0.07 + i * 0.6))
                cl  = qcol(C.BORDER_B, 170)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(cl))
            p.drawRoundedRect(QRectF(wx0 + i * bw, wy + 16 - hgt, bw - 2, max(2, hgt)), 2, 2)

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
        p.setFont(QFont(MONO_FONT, 14, QFont.Weight.Bold))
        p.drawText(QRectF(0, cy + r * 0.64, w, 30), Qt.AlignmentFlag.AlignCenter,
                   "MARK XLVIII ARC REACTOR")
        p.setFont(QFont(MONO_FONT, 10, QFont.Weight.Normal))
        p.setPen(QPen(qcol(C.TEXT_DIM, 200), 1))
        p.drawText(QRectF(0, cy + r * 0.74, w, 20), Qt.AlignmentFlag.AlignCenter,
                   f"ASSEMBLING CORE — {percent}%")

class MetricBar(QWidget):

    def __init__(self, label: str, color: str = C.PRI, parent=None):
        super().__init__(parent)
        self._label = label
        self._color = color
        self._value = 0.0       # 0–100
        self._text  = "--"
        self.setFixedHeight(38)
        self.setMinimumWidth(80)

    def set_value(self, pct: float, text: str):
        self._value = max(0.0, min(100.0, pct))
        self._text  = text
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()

        p.setBrush(QBrush(qcol(C.PANEL2, 160)))
        p.setPen(QPen(qcol(C.BORDER_A), 1))
        p.drawRoundedRect(QRectF(1, 1, W - 2, H - 2), 8, 8)

        bar_h   = 3
        bar_y   = H - bar_h - 6
        bar_w   = W - 12
        bar_x   = 6
        fill_w  = int(bar_w * self._value / 100)

        p.setBrush(QBrush(qcol(C.BAR_BG)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 2, 2)

        # one calm accent for normal readings — color only carries real
        # meaning here (warning / critical), it isn't decoration per metric
        if self._value > 85:
            bar_col = qcol(C.RED)
        elif self._value > 65:
            bar_col = qcol(C.ACC)
        else:
            bar_col = qcol(C.PRI, 200)

        if fill_w > 0:
            p.setBrush(QBrush(bar_col))
            p.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), 2, 2)

        p.setFont(QFont(MONO_FONT, 7, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(8, 5, 50, 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._label)

        p.setFont(QFont(MONO_FONT, 9, QFont.Weight.Bold))
        p.setPen(QPen(bar_col if self._text != "--" else qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(0, 4, W - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self._text)

class LogWidget(QTextEdit):
    _sig = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFont(UI_FONT, 9))
        self.setStyleSheet(f"""
            QTextEdit {{
                background: {C.PANEL2};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 14px;
                padding: 12px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 8px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B};
                border-radius: 4px;
                min-height: 20px;
            }}
        """)
        self._queue: list[str] = []
        self._typing  = False
        self._text    = ""
        self._pos     = 0
        self._tag     = "sys"
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._sig.connect(self._enqueue)

    def append_log(self, text: str):
        self._sig.emit(text)

    def _enqueue(self, text: str):
        self._queue.append(text)
        if not self._typing:
            self._next()

    def _next(self):
        if not self._queue:
            self._typing = False
            return
        self._typing = True
        self._text   = self._queue.pop(0)
        self._pos    = 0
        tl = self._text.lower()
        if   tl.startswith("you:"):    self._tag = "you"
        elif tl.startswith("jarvis:"): self._tag = "ai"
        elif tl.startswith("file:"):   self._tag = "file"
        elif "err" in tl:              self._tag = "err"
        else:                          self._tag = "sys"
        self._tmr.start(6)

    def _step(self):
        if self._pos < len(self._text):
            ch  = self._text[self._pos]
            cur = self.textCursor()
            fmt = cur.charFormat()
            col = {
                "you":  qcol(C.WHITE),
                "ai":   qcol(C.PRI),
                "err":  qcol(C.RED),
                "file": qcol(C.GREEN),
                "sys":  qcol(C.ACC2),
            }.get(self._tag, qcol(C.TEXT))
            fmt.setForeground(QBrush(col))
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText(ch, fmt)
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            self._pos += 1
        else:
            self._tmr.stop()
            cur = self.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText("\n")
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            QTimer.singleShot(20, self._next)

_FILE_ICONS = {
    "image":   ("🖼", "#00d4ff"), "video":   ("🎬", "#ff6b00"),
    "audio":   ("🎵", "#cc44ff"), "pdf":     ("📄", "#ff4444"),
    "word":    ("📝", "#4488ff"), "excel":   ("📊", "#44bb44"),
    "code":    ("💻", "#ffcc00"), "archive": ("📦", "#ff8844"),
    "pptx":    ("📊", "#ff6622"), "text":    ("📃", "#aaaaaa"),
    "data":    ("🔧", "#88ddff"), "unknown": ("📎", "#888888"),
}
_EXT_TO_CAT = {
    **dict.fromkeys(["jpg","jpeg","png","gif","webp","bmp","tiff","svg","ico"], "image"),
    **dict.fromkeys(["mp4","avi","mov","mkv","wmv","flv","webm","m4v"],         "video"),
    **dict.fromkeys(["mp3","wav","ogg","m4a","aac","flac","wma","opus"],        "audio"),
    **dict.fromkeys(["pdf"],                                                     "pdf"),
    **dict.fromkeys(["doc","docx"],                                              "word"),
    **dict.fromkeys(["xls","xlsx","ods"],                                        "excel"),
    **dict.fromkeys(["ppt","pptx"],                                              "pptx"),
    **dict.fromkeys(["py","js","ts","jsx","tsx","html","css","java","c","cpp",
                     "cs","go","rs","rb","php","swift","kt","sh","sql","lua"],   "code"),
    **dict.fromkeys(["zip","rar","tar","gz","7z","bz2","xz"],                   "archive"),
    **dict.fromkeys(["txt","md","rst","log"],                                    "text"),
    **dict.fromkeys(["csv","tsv","json","xml"],                                  "data"),
}

def _file_category(path: Path) -> str:
    return _EXT_TO_CAT.get(path.suffix.lower().lstrip("."), "unknown")

def _fmt_size(size: int) -> str:
    if   size < 1024:    return f"{size} B"
    elif size < 1024**2: return f"{size/1024:.1f} KB"
    elif size < 1024**3: return f"{size/1024**2:.1f} MB"
    else:                return f"{size/1024**3:.1f} GB"


class FileDropZone(QWidget):
    file_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(100)
        self._current_file: str | None = None
        self._hovering  = False
        self._drag_over = False
        self._dash_offset = 0.0
        self._anim_tmr = QTimer(self)
        self._anim_tmr.timeout.connect(self._animate)
        self._anim_tmr.start(40)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._canvas = _DropCanvas(self)
        layout.addWidget(self._canvas)

    def _animate(self):
        self._dash_offset = (self._dash_offset + 0.8) % 20
        self._canvas.update()

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._drag_over = True; self._canvas.update()

    def dragLeaveEvent(self, e):
        self._drag_over = False; self._canvas.update()

    def dropEvent(self, e: QDropEvent):
        self._drag_over = False
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if Path(path).is_file():
                self._set_file(path)
        self._canvas.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._browse()

    def enterEvent(self, e):
        self._hovering = True; self._canvas.update()

    def leaveEvent(self, e):
        self._hovering = False; self._canvas.update()

    def current_file(self) -> str | None:
        return self._current_file

    def clear_file(self):
        self._current_file = None; self._canvas.update()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a file for JARVIS", str(Path.home()),
            "All Files (*.*);;"
            "Images (*.jpg *.jpeg *.png *.gif *.webp *.bmp *.svg);;"
            "Documents (*.pdf *.docx *.txt *.md *.pptx);;"
            "Data (*.csv *.xlsx *.json *.xml);;"
            "Code (*.py *.js *.ts *.html *.css *.java *.cpp *.go);;"
            "Audio (*.mp3 *.wav *.ogg *.m4a *.aac *.flac);;"
            "Video (*.mp4 *.avi *.mov *.mkv *.wmv *.webm);;"
            "Archives (*.zip *.rar *.tar *.gz *.7z)",
        )
        if path:
            self._set_file(path)

    def _set_file(self, path: str):
        self._current_file = path
        self._canvas.update()
        self.file_selected.emit(path)


class _DropCanvas(QWidget):
    def __init__(self, zone: FileDropZone):
        super().__init__(zone)
        self._z = zone

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        z    = self._z
        W, H = self.width(), self.height()
        pad  = 6
        rect = QRectF(pad, pad, W - pad * 2, H - pad * 2)

        bg_col = qcol(C.PRI_GHO, 140) if z._drag_over else (qcol(C.PANEL2, 200) if z._hovering else qcol(C.PANEL2, 120))
        p.setBrush(QBrush(bg_col)); p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(rect, 14, 14)

        if z._current_file:   border_col = qcol(C.GREEN, 200)
        elif z._drag_over:    border_col = qcol(C.PRI, 230)
        elif z._hovering:     border_col = qcol(C.BORDER_B, 200)
        else:                 border_col = qcol(C.BORDER, 160)

        pen = QPen(border_col, 1.5, Qt.PenStyle.DashLine)
        pen.setDashOffset(z._dash_offset)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 14, 14)

        if z._current_file:   self._paint_file(p, W, H)
        elif z._drag_over:    self._paint_drag_over(p, W, H)
        else:                 self._paint_idle(p, W, H, z._hovering)

    def _paint_idle(self, p, W, H, hover):
        cx, cy = W / 2, H / 2
        col = qcol(C.PRI_DIM if not hover else C.PRI)
        p.setPen(QPen(col, 2)); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(cx, cy - 14), QPointF(cx, cy + 4))
        p.drawLine(QPointF(cx - 8, cy - 6), QPointF(cx, cy - 14))
        p.drawLine(QPointF(cx + 8, cy - 6), QPointF(cx, cy - 14))
        p.drawLine(QPointF(cx - 14, cy + 4), QPointF(cx + 14, cy + 4))
        p.setFont(QFont(MONO_FONT, 8))
        p.setPen(QPen(qcol(C.PRI_DIM if not hover else C.TEXT), 1))
        p.drawText(QRectF(0, cy + 8, W, 16), Qt.AlignmentFlag.AlignCenter,
                   "Drop file here  or  Click to Browse")
        p.setFont(QFont(MONO_FONT, 7))
        p.setPen(QPen(qcol("#1a4a5a"), 1))
        p.drawText(QRectF(0, cy + 24, W, 14), Qt.AlignmentFlag.AlignCenter,
                   "Images · Video · Audio · PDF · Docs · Code · Data")

    def _paint_drag_over(self, p, W, H):
        cx, cy = W / 2, H / 2
        p.setFont(QFont(MONO_FONT, 20))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy - 24, W, 32), Qt.AlignmentFlag.AlignCenter, "⬇")
        p.setFont(QFont(MONO_FONT, 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy + 12, W, 16), Qt.AlignmentFlag.AlignCenter, "Release to load")

    def _paint_file(self, p, W, H):
        path = Path(self._z._current_file)
        cat  = _file_category(path)
        icon, icon_col = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size_str = _fmt_size(path.stat().st_size)
        ext_str  = path.suffix.upper().lstrip(".") or "FILE"

        block_x, block_w = 10, 60
        p.setFont(QFont("Segoe UI Emoji", 22) if _OS == "Windows" else QFont("Arial", 22))
        p.setPen(QPen(qcol(icon_col), 1))
        p.drawText(QRectF(block_x, 0, block_w, H), Qt.AlignmentFlag.AlignCenter, icon)

        tx = block_x + block_w + 6
        tw = W - tx - 38

        p.setFont(QFont(MONO_FONT, 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.WHITE), 1))
        name = path.name if len(path.name) <= 34 else path.name[:31] + "..."
        p.drawText(QRectF(tx, H * 0.18, tw, 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        p.setFont(QFont(MONO_FONT, 7))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(tx, H * 0.18 + 18, tw, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"{ext_str}  ·  {size_str}")

        p.setFont(QFont(MONO_FONT, 6))
        p.setPen(QPen(qcol("#1e5c6a"), 1))
        par = str(path.parent)
        if len(par) > 42: par = "…" + par[-41:]
        p.drawText(QRectF(tx, H * 0.18 + 34, tw, 12),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, par)

        p.setFont(QFont(MONO_FONT, 9, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.RED, 180), 1))
        p.drawText(QRectF(W - 34, 0, 28, H), Qt.AlignmentFlag.AlignCenter, "✕")

    def mousePressEvent(self, e):
        z = self._z
        if z._current_file and e.pos().x() > self.width() - 34:
            z.clear_file()
        else:
            z.mousePressEvent(e)


class _CameraPreview(QWidget):
    """Floating overlay that briefly shows what the camera captured."""

    _W, _H = 244, 188

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            _CameraPreview {{
                background: rgba(0, 6, 10, 242);
                border: 1px solid {C.PRI};
                border-radius: 14px;
            }}
        """)
        self.setFixedWidth(self._W)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 5, 6, 6)
        lay.setSpacing(4)

        hdr = QHBoxLayout()
        title = QLabel("◈  VISUAL INPUT")
        title.setFont(QFont(UI_FONT, 7, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(title)
        hdr.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(16, 16)
        close_btn.setFont(QFont(MONO_FONT, 8))
        close_btn.setStyleSheet(
            f"color: {C.TEXT_DIM}; background: transparent; border: none;"
        )
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.hide)
        hdr.addWidget(close_btn)
        lay.addLayout(hdr)

        self._img_lbl = QLabel()
        self._img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_lbl.setStyleSheet("background: transparent;")
        lay.addWidget(self._img_lbl)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

        self.hide()

    def show_frame(self, img_bytes: bytes) -> None:
        px = QPixmap()
        px.loadFromData(img_bytes)
        if not px.isNull():
            max_w = self._W - 12
            scaled = px.scaled(
                max_w, 160,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._img_lbl.setPixmap(scaled)
            self._img_lbl.setFixedSize(scaled.width(), scaled.height())
            self.adjustSize()
        self.show()
        self.raise_()
        self._timer.start(6_000)   # auto-dismiss after 6 s


class SetupOverlay(QWidget):
    done = pyqtSignal(str, str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            SetupOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 16px;
            }}
        """)

        detected = {"darwin": "mac", "windows": "windows"}.get(
            _OS.lower(), "linux"
        )
        self._sel_os = detected

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 22, 30, 22)
        layout.setSpacing(8)

        def _lbl(txt, font_size=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont(UI_FONT, font_size,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        layout.addWidget(_lbl("◈  INITIALISATION REQUIRED", 13, True))
        layout.addWidget(_lbl("Configure J.A.R.V.I.S. before first boot.", 9, color=C.PRI_DIM))
        layout.addSpacing(6)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep)
        layout.addSpacing(4)

        layout.addWidget(_lbl("GEMINI API KEY", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        self._key_input = QLineEdit()
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setPlaceholderText("AIza…")
        self._key_input.setFont(QFont(MONO_FONT, 10))
        self._key_input.setFixedHeight(32)
        self._key_input.setStyleSheet(f"""
            QLineEdit {{
                background: #000d12; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 10px; padding: 4px 8px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        layout.addWidget(self._key_input)
        layout.addSpacing(12)

        sep_claude = QFrame(); sep_claude.setFrameShape(QFrame.Shape.HLine)
        sep_claude.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep_claude)
        layout.addSpacing(4)

        layout.addWidget(_lbl("CLAUDE API KEY  (optional)", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        self._claude_key_input = QLineEdit()
        self._claude_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._claude_key_input.setPlaceholderText("sk-ant-… (leave blank to skip for now)")
        self._claude_key_input.setFont(QFont(MONO_FONT, 10))
        self._claude_key_input.setFixedHeight(32)
        self._claude_key_input.setStyleSheet(f"""
            QLineEdit {{
                background: #000d12; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 10px; padding: 4px 8px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        layout.addWidget(self._claude_key_input)
        layout.addSpacing(12)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep2)
        layout.addSpacing(4)

        layout.addWidget(_lbl("OPERATING SYSTEM", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        det_name = {"windows": "Windows", "mac": "macOS", "linux": "Linux"}[detected]
        layout.addWidget(_lbl(f"Auto-detected: {det_name}", 8, color=C.ACC2,
                               align=Qt.AlignmentFlag.AlignLeft))

        os_row = QHBoxLayout(); os_row.setSpacing(6)
        self._os_btns: dict[str, QPushButton] = {}
        for key, label in [("windows","⊞  Windows"),("mac","  macOS"),("linux","🐧  Linux")]:
            btn = QPushButton(label)
            btn.setFont(QFont(UI_FONT, 9, QFont.Weight.Bold))
            btn.setFixedHeight(32)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, k=key: self._sel(k))
            os_row.addWidget(btn)
            self._os_btns[key] = btn
        layout.addLayout(os_row)
        self._sel(detected)
        layout.addSpacing(12)

        init_btn = QPushButton("▸  INITIALISE SYSTEMS")
        init_btn.setFont(QFont(UI_FONT, 10, QFont.Weight.Bold))
        init_btn.setFixedHeight(36)
        init_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        init_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 10px;
            }}
            QPushButton:hover {{
                background: {C.PRI_GHO}; border: 1px solid {C.PRI};
            }}
        """)
        init_btn.clicked.connect(self._submit)
        layout.addWidget(init_btn)

    def _sel(self, key: str):
        self._sel_os = key
        pal = {"windows":(C.PRI,"#001a22"),"mac":(C.ACC2,"#1a1400"),"linux":(C.GREEN,"#001a0d")}
        for k, btn in self._os_btns.items():
            if k == key:
                fg, bg = pal[k]
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {fg}; color: {bg};
                        border: none; border-radius: 10px; font-weight: bold;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: #000d12; color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 10px;
                    }}
                    QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
                """)

    def _submit(self):
        key = self._key_input.text().strip()
        if not key:
            self._key_input.setStyleSheet(
                self._key_input.styleSheet() +
                f" QLineEdit {{ border: 1px solid {C.RED}; }}"
            )
            return
        claude_key = self._claude_key_input.text().strip()
        self.done.emit(key, claude_key, self._sel_os)


class RemoteKeyOverlay(QWidget):
    """Floating overlay — QR code for instant phone pairing + manual key fallback."""

    closed = pyqtSignal()

    _OW, _OH = 400, 465

    def __init__(self, url: str, key: str, auto_login_url: str = "",
                 manual_url: str = "", expiry_secs: int = 600, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            RemoteKeyOverlay {{
                background: rgba(0, 4, 12, 0.95);
                border: 1px solid {C.BORDER_B};
                border-radius: 14px;
            }}
        """)
        self._expiry          = time.time() + expiry_secs
        self._on_new_key      = None
        self._auto_login_url  = auto_login_url
        self._manual_url      = manual_url or url

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 16, 24, 16)
        lay.setSpacing(5)

        def _lbl(txt, fs=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont(UI_FONT, fs,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            w.setWordWrap(True)
            return w

        lay.addWidget(_lbl("◈  REMOTE ACCESS", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep)

        # ── QR code ───────────────────────────────────────────────────────────
        self._qr_label = QLabel()
        self._qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_label.setFixedSize(176, 176)
        self._qr_label.setStyleSheet(
            "background: white; border-radius: 10px; padding: 4px;"
        )
        qr_row = QHBoxLayout()
        qr_row.addStretch()
        qr_row.addWidget(self._qr_label)
        qr_row.addStretch()
        lay.addLayout(qr_row)

        self._update_qr(auto_login_url)

        lay.addWidget(_lbl("Scan with phone camera to connect instantly", 8, color=C.TEXT_DIM))

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep2)

        lay.addWidget(_lbl("Or enter manually:", 7, color=C.TEXT_DIM,
                           align=Qt.AlignmentFlag.AlignLeft))

        self._url_lbl = QLabel(self._manual_url)
        self._url_lbl.setFont(QFont(MONO_FONT, 8))
        self._url_lbl.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        self._url_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._url_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self._url_lbl)

        self._key_lbl = QLabel(key)
        self._key_lbl.setFont(QFont(MONO_FONT, 28, QFont.Weight.Bold))
        self._key_lbl.setStyleSheet(f"""
            color: {C.ACC};
            background: {C.PANEL2};
            border: 1px solid {C.BORDER_B};
            border-radius: 8px;
            padding: 6px 4px;
            letter-spacing: 10px;
        """)
        self._key_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._key_lbl)

        self._timer_lbl = QLabel()
        self._timer_lbl.setFont(QFont(MONO_FONT, 8))
        self._timer_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._timer_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._timer_lbl)

        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        new_btn = QPushButton("NEW KEY")
        new_btn.setFixedHeight(32)
        new_btn.setFont(QFont(UI_FONT, 8, QFont.Weight.Bold))
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 16px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        new_btn.clicked.connect(self._refresh_key)
        btn_row.addWidget(new_btn)

        close_btn = QPushButton("DISMISS")
        close_btn.setFixedHeight(32)
        close_btn.setFont(QFont(UI_FONT, 8, QFont.Weight.Bold))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 16px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self._do_close)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)

        self._ctimer = QTimer(self)
        self._ctimer.timeout.connect(self._tick)
        self._ctimer.start(1000)
        self._tick()

    def set_new_key_callback(self, fn) -> None:
        self._on_new_key = fn

    def _update_qr(self, url: str) -> None:
        if not url:
            self._qr_label.setText("—")
            return
        try:
            import qrcode as _qrmod
            from io import BytesIO
            qr = _qrmod.QRCode(
                box_size=5, border=2,
                error_correction=_qrmod.constants.ERROR_CORRECT_M,
            )
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = BytesIO()
            img.save(buf, format="PNG")
            px = QPixmap()
            px.loadFromData(buf.getvalue())
            self._qr_label.setPixmap(
                px.scaled(170, 170,
                          Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
            )
        except ImportError:
            self._qr_label.setText("pip install\nqrcode[pil]")
            self._qr_label.setFont(QFont(MONO_FONT, 8))
            self._qr_label.setStyleSheet(
                "color: #888; background: white; border-radius: 10px; padding: 4px;"
            )
        except Exception:
            self._qr_label.setText(url[:28])
            self._qr_label.setFont(QFont(MONO_FONT, 7))
            self._qr_label.setStyleSheet(
                f"color: {C.PRI}; background: white; border-radius: 10px; padding: 4px;"
            )

    def _tick(self):
        remaining = max(0, int(self._expiry - time.time()))
        m, s = divmod(remaining, 60)
        self._timer_lbl.setText(f"Key expires in  {m:02d}:{s:02d}")
        if remaining == 0:
            self._do_close()

    def mark_connected(self) -> None:
        """Call from any thread when a phone successfully connects."""
        self._ctimer.stop()
        self._key_lbl.setText("CONNECTED")
        self._key_lbl.setStyleSheet(f"""
            color: {C.GREEN};
            background: rgba(34,197,94,0.08);
            border: 2px solid rgba(34,197,94,0.4);
            border-radius: 8px;
            padding: 6px 4px;
            letter-spacing: 4px;
        """)
        self._qr_label.setText("✓")
        self._qr_label.setFont(QFont(MONO_FONT, 54, QFont.Weight.Bold))
        self._qr_label.setStyleSheet(
            "color: #00ff88; background: #001a0d; border-radius: 10px;"
        )
        self._timer_lbl.setText("Phone connected — JARVIS ready")
        self._timer_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")

    def _refresh_key(self):
        if self._on_new_key:
            result = self._on_new_key()
            if result:
                url    = result[0]
                key    = result[1]
                auto   = result[2] if len(result) >= 3 else ""
                manual = result[3] if len(result) >= 4 else url
                self._manual_url     = manual or url
                self._url_lbl.setText(self._manual_url)
                self._key_lbl.setText(key)
                self._auto_login_url = auto
                self._update_qr(auto or url)
                self._expiry = time.time() + 600
                self._key_lbl.setStyleSheet(f"""
                    color: {C.ACC};
                    background: {C.PANEL2};
                    border: 1px solid {C.BORDER_B};
                    border-radius: 8px;
                    padding: 6px 4px;
                    letter-spacing: 10px;
                """)
                self._timer_lbl.setStyleSheet(
                    f"color: {C.TEXT_MED}; background: transparent;"
                )
                self._ctimer.start(1000)
                self._tick()

    def _do_close(self):
        self._ctimer.stop()
        self.hide()
        self.closed.emit()


class MainWindow(QMainWindow):
    _log_sig     = pyqtSignal(str)
    _state_sig   = pyqtSignal(str)
    _content_sig = pyqtSignal(str, str)   # (title, text) — thread-safe content display
    _status_sig  = pyqtSignal(dict)       # subsystem constellation status — thread-safe
    _reconfig_sig = pyqtSignal()          # trigger setup overlay from any thread
    _camera_sig     = pyqtSignal(bytes)   # show camera frame preview (small overlay)
    _cam_stream_sig = pyqtSignal(bool)   # True=start live stream, False=stop
    _cam_frame_sig  = pyqtSignal(bytes)  # live camera frame → HUD area

    def __init__(self, face_path: str):
        super().__init__()
        self._face_path = face_path
        self.setWindowTitle("J.A.R.V.I.S — MARK XLVIII")
        self.setMinimumSize(_MIN_W, _MIN_H)
        self.resize(_DEFAULT_W, _DEFAULT_H)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(
            (screen.width()  - _DEFAULT_W) // 2,
            (screen.height() - _DEFAULT_H) // 2,
        )

        self.on_text_command   = None
        self.on_remote_clicked = None   # callable: () -> (url, key) | None
        self.on_interrupt      = None   # callable: () -> None — stop JARVIS mid-speech
        self._muted            = False
        self._barge_in         = False  # voice barge-in — off by default (needs headphones to work well)
        self._current_file: str | None = None
        self._remote_overlay: RemoteKeyOverlay | None = None

        central = QWidget()
        central.setStyleSheet(f"background: {C.BG};")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self._left_panel = self._build_left_panel()
        body.addWidget(self._left_panel, stretch=0)

        # Center column: HUD + resizable content panel via QSplitter
        self.hud = HudCanvas(face_path)
        self.hud.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._content_panel = self._build_content_panel()

        # Live camera container — replaces HUD when camera stream is active
        _cam_cont = QWidget()
        _cam_cont.setStyleSheet("background: #000308;")
        _cam_v = QVBoxLayout(_cam_cont)
        _cam_v.setContentsMargins(0, 0, 0, 0)
        _cam_v.setSpacing(0)
        _cam_hdr = QHBoxLayout()
        _cam_hdr.setContentsMargins(8, 5, 8, 5)
        _cam_title = QLabel("◈  CAMERA FEED")
        _cam_title.setFont(QFont(UI_FONT, 8, QFont.Weight.Bold))
        _cam_title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        _cam_hdr.addWidget(_cam_title)
        _cam_hdr.addStretch()
        _cam_x = QPushButton("✕  CLOSE")
        _cam_x.setFont(QFont(MONO_FONT, 8, QFont.Weight.Bold))
        _cam_x.setCursor(Qt.CursorShape.PointingHandCursor)
        _cam_x.setStyleSheet(f"""
            QPushButton {{
                color: {C.TEXT_DIM}; background: transparent;
                border: none; padding: 2px 6px;
            }}
            QPushButton:hover {{ color: {C.PRI}; }}
        """)
        _cam_x.clicked.connect(self.stop_camera_stream)
        _cam_hdr.addWidget(_cam_x)
        _cam_v.addLayout(_cam_hdr)
        self._cam_live_lbl = QLabel()
        self._cam_live_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cam_live_lbl.setStyleSheet("background: transparent;")
        self._cam_live_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        _cam_v.addWidget(self._cam_live_lbl, stretch=1)

        # Stack: 0 = animated HUD, 1 = live camera
        self._hud_cam_stack = QStackedWidget()
        self._hud_cam_stack.addWidget(self.hud)
        self._hud_cam_stack.addWidget(_cam_cont)

        self._center_split = QSplitter(Qt.Orientation.Vertical)
        self._center_split.setStyleSheet(f"""
            QSplitter::handle {{
                background: {C.BORDER};
                height: 4px;
            }}
            QSplitter::handle:hover {{
                background: {C.PRI_DIM};
            }}
        """)
        self._center_split.addWidget(self._hud_cam_stack)
        self._center_split.addWidget(self._content_panel)
        self._center_split.setStretchFactor(0, 3)
        self._center_split.setStretchFactor(1, 1)
        self._center_split.setCollapsible(0, False)
        body.addWidget(self._center_split, stretch=5)

        self._right_panel = self._build_right_panel()
        body.addWidget(self._right_panel, stretch=0)

        root.addLayout(body, stretch=1)
        root.addWidget(self._build_footer())

        self._clock_tmr = QTimer(self)
        self._clock_tmr.timeout.connect(self._tick_clock)
        self._clock_tmr.start(1000)
        self._tick_clock()

        # Metrik güncelleme timer'ı
        self._metric_tmr = QTimer(self)
        self._metric_tmr.timeout.connect(self._update_metrics)
        self._metric_tmr.start(2000)
        self._update_metrics()

        self._log_sig.connect(self._log.append_log)
        self._state_sig.connect(self._apply_state)
        self._content_sig.connect(self._show_content)
        self._status_sig.connect(self.hud.set_subsystem_status)
        self.hud.node_clicked.connect(self._on_node_clicked)
        self._reconfig_sig.connect(self._show_setup)
        self._camera_sig.connect(self._show_camera_frame)
        self._cam_stream_sig.connect(self._on_cam_stream)
        self._cam_frame_sig.connect(self._on_cam_frame)
        self._cam_stop = threading.Event()

        # Camera preview overlay (child of central widget, positioned in resizeEvent)
        self._cam_preview = _CameraPreview(self.centralWidget())

        self._overlay: SetupOverlay | None = None
        self._ready = self._check_config()
        if not self._ready:
            self._show_setup()

        sc_mute = QShortcut(QKeySequence("F4"), self)
        sc_mute.activated.connect(self._toggle_mute)
        sc_full = QShortcut(QKeySequence("F11"), self)
        sc_full.activated.connect(self._toggle_fullscreen)
        sc_intr = QShortcut(QKeySequence("Escape"), self)
        sc_intr.activated.connect(self._do_interrupt)

    def _show_camera_frame(self, img_bytes: bytes):
        """Slot — display camera preview overlay (main thread)."""
        self._cam_preview.show_frame(img_bytes)
        cw = self.centralWidget()
        pw = _CameraPreview._W
        ph = self._cam_preview.height()
        self._cam_preview.setGeometry(
            cw.width() - _RIGHT_W - pw - 12,
            cw.height() - ph - 28,
            pw, ph,
        )

    # --- Live camera stream in HUD area ------------------------------------
    def _on_cam_stream(self, start: bool) -> None:
        if start:
            self._hud_cam_stack.setCurrentIndex(1)
        else:
            self._hud_cam_stack.setCurrentIndex(0)
            self._cam_live_lbl.clear()

    def _on_cam_frame(self, data: bytes) -> None:
        px = QPixmap()
        px.loadFromData(data)
        if not px.isNull():
            w, h = self._cam_live_lbl.width(), self._cam_live_lbl.height()
            if w > 1 and h > 1:
                self._cam_live_lbl.setPixmap(
                    px.scaled(w, h,
                              Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
                )

    def start_camera_stream(self) -> None:
        self._cam_stop.clear()
        self._cam_stream_sig.emit(True)
        t = threading.Thread(target=self._cam_loop, daemon=True, name="cam-stream")
        t.start()

    def _cam_loop(self) -> None:
        try:
            import cv2
            # Reuse camera index detected by screen_processor (cached in api_keys.json)
            cam_idx = 0
            try:
                import json as _j
                cfg = _j.loads((CONFIG_DIR / "api_keys.json").read_text())
                cam_idx = int(cfg.get("camera_index", 0))
            except Exception:
                pass
            try:
                backend = cv2.CAP_DSHOW if _OS == "Windows" else cv2.CAP_ANY
            except AttributeError:
                backend = 0
            cap = cv2.VideoCapture(cam_idx, backend)
            if not cap.isOpened():
                cap = cv2.VideoCapture(0)
            if not cap.isOpened():
                return
            # warm-up frames
            for _ in range(5):
                cap.read()
            while not self._cam_stop.wait(0.033) and cap.isOpened():
                ret, frame = cap.read()
                if ret and frame is not None:
                    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
                    self._cam_frame_sig.emit(buf.tobytes())
            cap.release()
        except Exception as e:
            print(f"[Camera] Stream error: {e}")
        finally:
            self._cam_stream_sig.emit(False)

    def stop_camera_stream(self) -> None:
        self._cam_stop.set()

    # ------------------------------------------------------------------
    # Icon generation — arc-reactor style, rendered with Pillow
    # ------------------------------------------------------------------
    @staticmethod
    def _build_jarvis_icon(out_path: Path) -> bool:
        """
        Render a JARVIS arc-reactor icon at 4× resolution and downsample
        for crisp results at all sizes. Saves a multi-res .ico to out_path.
        Returns True on success.
        """
        try:
            import math
            import PIL.Image
            import PIL.ImageDraw
            import PIL.ImageFilter
        except ImportError:
            return False

        CYAN   = (0, 212, 255)
        DIM    = (0, 100, 140)
        DARK   = (0, 6, 10)
        GLOW   = (0, 160, 200)
        WHITE  = (220, 240, 255)

        def _render(sz: int) -> PIL.Image.Image:
            S  = sz * 4                     # draw at 4× then downscale
            img = PIL.Image.new("RGBA", (S, S), (0, 0, 0, 0))
            d   = PIL.ImageDraw.Draw(img)
            cx = cy = S // 2

            # ── filled background circle ──────────────────────────────────
            R = S // 2 - 2
            d.ellipse([cx-R, cy-R, cx+R, cy+R], fill=(*DARK, 255))

            # ── outer border ring ─────────────────────────────────────────
            lw = max(2, S // 40)
            d.ellipse([cx-R, cy-R, cx+R, cy+R],
                      outline=(*CYAN, 220), width=lw)

            # ── mid decorative ring ───────────────────────────────────────
            R2 = int(R * 0.72)
            d.ellipse([cx-R2, cy-R2, cx+R2, cy+R2],
                      outline=(*DIM, 180), width=max(1, lw // 2))

            # ── 6 radial spokes (hex bolt) ────────────────────────────────
            R_inner = int(R * 0.30)
            R_outer = int(R * 0.62)
            spoke_w = max(1, S // 80)
            for i in range(6):
                angle = math.radians(i * 60 - 30)
                x1 = cx + int(R_inner * math.cos(angle))
                y1 = cy + int(R_inner * math.sin(angle))
                x2 = cx + int(R_outer * math.cos(angle))
                y2 = cy + int(R_outer * math.sin(angle))
                d.line([x1, y1, x2, y2], fill=(*GLOW, 200), width=spoke_w)

            # ── 6 tick marks on outer ring ────────────────────────────────
            for i in range(6):
                angle = math.radians(i * 60)
                for dr in range(lw * 2):
                    rx = (R - lw - dr)
                    d.point(
                        [cx + int(rx * math.cos(angle)),
                         cy + int(rx * math.sin(angle))],
                        fill=(*WHITE, 220),
                    )

            # ── inner glowing ring ────────────────────────────────────────
            Ri = int(R * 0.26)
            d.ellipse([cx-Ri, cy-Ri, cx+Ri, cy+Ri],
                      outline=(*CYAN, 255), width=max(2, lw))

            # ── bright glow soft blur applied before core ─────────────────
            # (draw a slightly larger cyan circle on a separate layer)
            glow_layer = PIL.Image.new("RGBA", (S, S), (0, 0, 0, 0))
            gd = PIL.ImageDraw.Draw(glow_layer)
            Rc = int(R * 0.13)
            gd.ellipse([cx-Rc*2, cy-Rc*2, cx+Rc*2, cy+Rc*2],
                       fill=(*CYAN, 110))
            glow_layer = glow_layer.filter(PIL.ImageFilter.GaussianBlur(S // 14))
            img = PIL.Image.alpha_composite(img, glow_layer)
            d   = PIL.ImageDraw.Draw(img)

            # ── core dot ──────────────────────────────────────────────────
            d.ellipse([cx-Rc, cy-Rc, cx+Rc, cy+Rc], fill=(*WHITE, 255))

            # ── downscale to target size ──────────────────────────────────
            return img.resize((sz, sz), PIL.Image.LANCZOS)

        try:
            sizes  = [256, 128, 64, 48, 32, 16]
            frames = [_render(s) for s in sizes]
            frames[0].save(
                out_path,
                format="ICO",
                append_images=frames[1:],
                sizes=[(s, s) for s in sizes],
            )
            return True
        except Exception as e:
            print(f"[Shortcut] ⚠️  Icon generation failed: {e}")
            return False

    @staticmethod
    def _create_lnk_windows(lnk: str, target: str, args: str,
                             work_dir: str, icon_loc: str) -> None:
        """
        Create a Windows .lnk shortcut WITHOUT launching PowerShell or cmd.
        Tries win32com (pywin32) first; falls back to wscript.exe + VBScript.
        wscript.exe is a GUI-mode host — it never opens a console window.
        """
        # ── Option 1: pywin32 (pure Python COM, zero subprocess) ──────────
        try:
            from win32com.client import Dispatch   # type: ignore
            sh = Dispatch("WScript.Shell")
            sc = sh.CreateShortCut(lnk)
            sc.TargetPath       = target
            sc.Arguments        = f'"{args}"'
            sc.WorkingDirectory = work_dir
            sc.Description      = "J.A.R.V.I.S AI Assistant"
            sc.IconLocation     = icon_loc
            sc.save()
            return
        except ImportError:
            pass

        # ── Option 2: wscript.exe + VBScript (always available on Windows,
        #    GUI-mode executable — never opens a console window) ────────────
        vbs = "\n".join([
            'Set ws = CreateObject("WScript.Shell")',
            f'Set sc = ws.CreateShortcut("{lnk}")',
            f'sc.TargetPath = "{target}"',
            f'sc.Arguments = Chr(34) & "{args}" & Chr(34)',
            f'sc.WorkingDirectory = "{work_dir}"',
            'sc.Description = "J.A.R.V.I.S AI Assistant"',
            f'sc.IconLocation = "{icon_loc}"',
            'sc.Save',
        ])
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".vbs")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(vbs)
            proc = subprocess.Popen(
                ["wscript.exe", "/nologo", tmp],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
            )
            proc.wait(timeout=10)
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    def _create_desktop_shortcut(self):
        """
        Create a desktop shortcut on Windows / macOS / Linux.
        Never opens a terminal, console, or PowerShell window on any platform.
        """
        import stat as _stat
        script  = Path(__file__).resolve().parent / "main.py"
        python  = Path(sys.executable)
        desktop = Path.home() / "Desktop"

        # Arc-reactor icon (.ico — also exported as .png for Linux/macOS)
        ico_path = resource_path("config/jarvis.ico")
        if not ico_path.exists():
            self._build_jarvis_icon(ico_path)

        try:
            _os = platform.system()

            # ── Windows ───────────────────────────────────────────────────────
            if _os == "Windows":
                pythonw  = python.parent / "pythonw.exe"
                target   = str(pythonw if pythonw.exists() else python)
                lnk      = str(desktop / "J.A.R.V.I.S.lnk")
                icon_loc = str(ico_path) if ico_path.exists() else f"{target},0"
                self._create_lnk_windows(lnk, target, str(script),
                                         str(script.parent), icon_loc)

            # ── macOS — proper .app bundle (no Terminal window) ───────────────
            elif _os == "Darwin":
                app     = desktop / "J.A.R.V.I.S.app"
                mac_dir = app / "Contents" / "MacOS"
                res_dir = app / "Contents" / "Resources"
                mac_dir.mkdir(parents=True, exist_ok=True)
                res_dir.mkdir(exist_ok=True)

                # Launcher executable (bash — runs as background process,
                # macOS does NOT open Terminal for executables inside .app bundles)
                launcher = mac_dir / "JARVIS"
                launcher.write_text(
                    "#!/usr/bin/env bash\n"
                    f'cd "{script.parent}"\n'
                    f'exec "{python}" "{script}"\n'
                )
                launcher.chmod(launcher.stat().st_mode
                               | _stat.S_IEXEC | _stat.S_IXGRP | _stat.S_IXOTH)

                # Minimal Info.plist (required for .app recognition)
                (app / "Contents" / "Info.plist").write_text(
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                    '<plist version="1.0"><dict>\n'
                    '  <key>CFBundleExecutable</key><string>JARVIS</string>\n'
                    '  <key>CFBundleIdentifier</key>'
                    '<string>com.jarvis.assistant</string>\n'
                    '  <key>CFBundleName</key><string>J.A.R.V.I.S</string>\n'
                    '  <key>CFBundlePackageType</key><string>APPL</string>\n'
                    '  <key>CFBundleVersion</key><string>1.0</string>\n'
                    '</dict></plist>\n'
                )

                # Optional: copy icon as .icns (skip silently if Pillow is missing)
                try:
                    import PIL.Image
                    icns = res_dir / "AppIcon.icns"
                    PIL.Image.open(ico_path).save(icns, format="ICNS")
                    # Inject icon reference into plist
                    plist = app / "Contents" / "Info.plist"
                    txt = plist.read_text()
                    plist.write_text(
                        txt.replace(
                            '</dict></plist>',
                            '  <key>CFBundleIconFile</key>'
                            '<string>AppIcon</string>\n</dict></plist>\n',
                        )
                    )
                except Exception:
                    pass  # icon is optional

            # ── Linux — .desktop file (Terminal=false, no console) ────────────
            else:
                # Export .ico → .png for better desktop integration
                png_path = ico_path.with_suffix(".png")
                if not png_path.exists() and ico_path.exists():
                    try:
                        import PIL.Image
                        PIL.Image.open(ico_path).resize(
                            (256, 256), PIL.Image.LANCZOS
                        ).save(png_path, format="PNG")
                    except Exception:
                        png_path = ico_path  # fallback to .ico

                icon_line = f"Icon={png_path}\n" if png_path.exists() else ""
                desk = desktop / "J.A.R.V.I.S.desktop"
                desk.write_text(
                    "[Desktop Entry]\n"
                    "Name=J.A.R.V.I.S\n"
                    f"Exec={python} {script}\n"
                    f"Path={script.parent}\n"
                    "Type=Application\n"
                    "Terminal=false\n"
                    "Categories=Utility;\n"
                    + icon_line
                )
                desk.chmod(desk.stat().st_mode | 0o755)

            self._log.append_log("SYS: Desktop shortcut created.")
        except Exception as e:
            self._log.append_log(f"ERR: Shortcut failed — {e}")

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cw = self.centralWidget()
        if self._overlay and self._overlay.isVisible():
            ow, oh = 460, 390
            self._overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if self._remote_overlay and self._remote_overlay.isVisible():
            ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
            self._remote_overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        # Camera preview — bottom-right corner of the center/HUD area
        pw = _CameraPreview._W
        ph = self._cam_preview.height() or _CameraPreview._H
        self._cam_preview.setGeometry(
            cw.width() - _RIGHT_W - pw - 12,
            cw.height() - ph - 28,
            pw, ph,
        )

    def _update_metrics(self):
        snap = _metrics.snapshot()

        # CPU
        cpu = snap["cpu"]
        self._bar_cpu.set_value(cpu, f"{cpu:.0f}%")

        # MEM
        mem = snap["mem"]
        self._bar_mem.set_value(mem, f"{mem:.0f}%")

        # NET
        net = snap["net"]
        if net < 1.0:
            net_str = f"{net*1024:.0f}KB/s"
        else:
            net_str = f"{net:.1f}MB/s"
        net_pct = min(100, net * 10)  # 10 MB/s = %100
        self._bar_net.set_value(net_pct, net_str)

        # GPU
        gpu = snap["gpu"]
        if gpu >= 0:
            self._bar_gpu.set_value(gpu, f"{gpu:.0f}%")
        else:
            self._bar_gpu.set_value(0, "N/A")

        # TMP
        tmp = snap["tmp"]
        if tmp >= 0:
            tmp_pct = min(100, (tmp / 100) * 100)
            self._bar_tmp.set_value(tmp_pct, f"{tmp:.0f}°C")
        else:
            self._bar_tmp.set_value(0, "N/A")

        try:
            boot_t  = psutil.boot_time()
            elapsed = time.time() - boot_t
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            self._uptime_lbl.setText(f"UP  {h:02d}:{m:02d}")
        except Exception:
            self._uptime_lbl.setText("UP  --:--")

        try:
            proc_count = len(psutil.pids())
            self._proc_lbl.setText(f"PROC  {proc_count}")
        except Exception:
            self._proc_lbl.setText("PROC  --")


    def _build_header(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(54)
        w.setStyleSheet(f"background: {C.DARK}; border-bottom: 1px solid {C.BORDER_B};")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(16, 0, 16, 0)

        def _badge(txt, color=C.TEXT_MED):
            l = QLabel(txt)
            l.setFont(QFont(UI_FONT, 8))
            l.setStyleSheet(f"color: {color}; background: transparent;")
            return l

        lay.addWidget(_badge("MARK XLVIII", C.PRI_DIM))
        lay.addStretch()

        mid = QVBoxLayout(); mid.setSpacing(1)
        title = QLabel("J.A.R.V.I.S")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setFont(QFont(UI_FONT, 17, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        mid.addWidget(title)
        sub = QLabel("Just A Rather Very Intelligent System")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setFont(QFont(UI_FONT, 7))
        sub.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        mid.addWidget(sub)
        lay.addLayout(mid)
        lay.addStretch()

        right_col = QVBoxLayout(); right_col.setSpacing(2)
        self._clock_lbl = QLabel("00:00:00")
        self._clock_lbl.setFont(QFont(MONO_FONT, 14, QFont.Weight.Bold))
        self._clock_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        self._clock_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._clock_lbl)
        self._date_lbl = QLabel("")
        self._date_lbl.setFont(QFont(MONO_FONT, 7))
        self._date_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._date_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._date_lbl)
        lay.addLayout(right_col)
        return w

    def _tick_clock(self):
        self._clock_lbl.setText(time.strftime("%H:%M:%S"))
        self._date_lbl.setText(time.strftime("%a %d %b %Y"))

    def _build_left_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_LEFT_W)
        w.setStyleSheet(f"background: {C.PANEL}; border-right: 1px solid {C.BORDER};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 14, 12, 14)
        lay.setSpacing(10)

        hdr = QLabel("SYSTEM")
        hdr.setFont(QFont(UI_FONT, 9, QFont.Weight.DemiBold))
        hdr.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; letter-spacing: 1px;")
        lay.addWidget(hdr)

        bars = QVBoxLayout(); bars.setSpacing(8)
        self._bar_cpu = MetricBar("CPU")
        self._bar_mem = MetricBar("MEM")
        self._bar_net = MetricBar("NET")
        self._bar_gpu = MetricBar("GPU")
        self._bar_tmp = MetricBar("TMP")
        for bar in [self._bar_cpu, self._bar_mem, self._bar_net,
                    self._bar_gpu, self._bar_tmp]:
            bars.addWidget(bar)
        lay.addLayout(bars)

        info_panel = QWidget()
        info_panel.setStyleSheet(
            f"background: {C.PANEL2}; border: 1px solid {C.BORDER}; border-radius: 12px;"
        )
        ip_lay = QVBoxLayout(info_panel)
        ip_lay.setContentsMargins(10, 9, 10, 9)
        ip_lay.setSpacing(4)

        self._uptime_lbl = QLabel("Up --:--")
        self._uptime_lbl.setFont(QFont(MONO_FONT, 9))
        self._uptime_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent; border: none;")
        ip_lay.addWidget(self._uptime_lbl)

        self._proc_lbl = QLabel("-- processes")
        self._proc_lbl.setFont(QFont(MONO_FONT, 9))
        self._proc_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; border: none;")
        ip_lay.addWidget(self._proc_lbl)

        os_name = {"Windows": "Windows", "Darwin": "macOS", "Linux": "Linux"}.get(_OS, _OS)
        os_lbl = QLabel(os_name)
        os_lbl.setFont(QFont(MONO_FONT, 9))
        os_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; border: none;")
        ip_lay.addWidget(os_lbl)

        lay.addWidget(info_panel)
        lay.addStretch()

        for txt, col in [("Core active", C.GREEN), ("Secure", C.PRI)]:
            row = QHBoxLayout(); row.setSpacing(7)
            dot = QLabel("●")
            dot.setFont(QFont(UI_FONT, 7))
            dot.setStyleSheet(f"color: {col}; background: transparent;")
            dot.setFixedWidth(10)
            row.addWidget(dot)
            lbl = QLabel(txt)
            lbl.setFont(QFont(UI_FONT, 9, QFont.Weight.DemiBold))
            lbl.setStyleSheet(f"color: {col}; background: transparent;")
            row.addWidget(lbl)
            row.addStretch()
            lay.addLayout(row)

        return w

    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_RIGHT_W)
        w.setStyleSheet(f"background: {C.PANEL}; border-left: 1px solid {C.BORDER};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        def _sec(txt):
            l = QLabel(txt)
            l.setFont(QFont(UI_FONT, 9, QFont.Weight.DemiBold))
            l.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; letter-spacing: 1px;")
            return l

        lay.addWidget(_sec("ACTIVITY"))
        self._log = LogWidget()
        lay.addWidget(self._log, stretch=1)

        lay.addWidget(_sec("FILE UPLOAD"))
        self._drop_zone = FileDropZone()
        self._drop_zone.file_selected.connect(self._on_file_selected)
        lay.addWidget(self._drop_zone)

        self._file_hint = QLabel("No file loaded — drop or click above to upload")
        self._file_hint.setFont(QFont(UI_FONT, 9))
        self._file_hint.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._file_hint.setWordWrap(True)
        lay.addWidget(self._file_hint)

        lay.addLayout(self._build_input_row())

        self._interrupt_btn = QPushButton("Interrupt  ·  Esc")
        self._interrupt_btn.setFixedHeight(40)
        self._interrupt_btn.setFont(QFont(UI_FONT, 10, QFont.Weight.DemiBold))
        self._interrupt_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._interrupt_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255,157,157,0.10); color: {C.MUTED_C};
                border: 1px solid rgba(255,157,157,0.35); border-radius: 20px;
            }}
            QPushButton:hover {{
                background: rgba(255,157,157,0.16); border: 1px solid rgba(255,157,157,0.5);
            }}
            QPushButton:pressed {{
                background: rgba(255,157,157,0.22);
            }}
        """)
        self._interrupt_btn.clicked.connect(self._do_interrupt)
        lay.addWidget(self._interrupt_btn)

        self._mute_btn = QPushButton("Microphone active")
        self._mute_btn.setFixedHeight(40)
        self._mute_btn.setFont(QFont(UI_FONT, 10, QFont.Weight.DemiBold))
        self._mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mute_btn.clicked.connect(self._toggle_mute)
        self._style_mute_btn()
        lay.addWidget(self._mute_btn)

        self._barge_in_btn = QPushButton("Voice barge-in: off")
        self._barge_in_btn.setFixedHeight(40)
        self._barge_in_btn.setFont(QFont(UI_FONT, 10, QFont.Weight.DemiBold))
        self._barge_in_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._barge_in_btn.setToolTip(
            "Lets you talk over JARVIS to interrupt it, instead of pressing Esc.\n"
            "Only enable this if you're on headphones — on open speakers the mic\n"
            "will hear JARVIS's own voice and interrupt it constantly."
        )
        self._barge_in_btn.clicked.connect(self._toggle_barge_in)
        self._style_barge_in_btn()
        lay.addWidget(self._barge_in_btn)

        remote_btn = QPushButton("Remote control")
        remote_btn.setFixedHeight(36)
        remote_btn.setFont(QFont(UI_FONT, 9, QFont.Weight.DemiBold))
        remote_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remote_btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba(127,227,255,0.08); color: {C.PRI};
                border: 1px solid rgba(127,227,255,0.30); border-radius: 18px;
            }}
            QPushButton:hover {{
                background: rgba(127,227,255,0.14); border: 1px solid rgba(127,227,255,0.5);
            }}
        """)
        remote_btn.clicked.connect(self._open_remote)
        lay.addWidget(remote_btn)

        fs_btn = QPushButton("Fullscreen  ·  F11")
        fs_btn.setFixedHeight(28)
        fs_btn.setFont(QFont(UI_FONT, 8))
        fs_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        fs_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 14px;
            }}
            QPushButton:hover {{
                color: {C.PRI}; border: 1px solid {C.BORDER_B};
            }}
        """)
        fs_btn.clicked.connect(self._toggle_fullscreen)
        lay.addWidget(fs_btn)

        sc_btn = QPushButton("Create desktop shortcut")
        sc_btn.setFixedHeight(28)
        sc_btn.setFont(QFont(UI_FONT, 8))
        sc_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        sc_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 14px;
            }}
            QPushButton:hover {{
                color: {C.TEXT}; border: 1px solid {C.BORDER_B};
            }}
        """)
        sc_btn.clicked.connect(self._create_desktop_shortcut)
        lay.addWidget(sc_btn)

        return w

    def _build_input_row(self) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(6)
        self._input = QLineEdit()
        self._input.setPlaceholderText("Type a command…")
        self._input.setFont(QFont(UI_FONT, 10))
        self._input.setFixedHeight(38)
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: {C.PANEL2}; color: {C.WHITE};
                border: 1px solid {C.BORDER}; border-radius: 12px; padding: 3px 12px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI_DIM}; }}
        """)
        self._input.returnPressed.connect(self._send)
        row.addWidget(self._input)

        send = QPushButton("→")
        send.setFixedSize(38, 38)
        send.setFont(QFont(UI_FONT, 13, QFont.Weight.DemiBold))
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL2}; color: {C.PRI};
                border: 1px solid {C.BORDER}; border-radius: 19px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI_DIM}; }}
        """)
        send.clicked.connect(self._send)
        row.addWidget(send)
        return row

    def _build_content_panel(self) -> QWidget:
        """
        Collapsible panel below the HUD — shows search results, news, briefings.
        Hidden by default; appears when show_content() is called.
        """
        w = QWidget()
        w.setObjectName("ContentPanel")
        w.setStyleSheet(f"""
            QWidget#ContentPanel {{
                background: {C.PANEL};
                border-top: 1px solid {C.BORDER_B};
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 7, 12, 8)
        lay.setSpacing(5)

        # ── header row ───────────────────────────────────────────────────────
        hdr = QHBoxLayout(); hdr.setSpacing(6)

        dot = QLabel("◈")
        dot.setFont(QFont(MONO_FONT, 9, QFont.Weight.Bold))
        dot.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(dot)

        self._content_title_lbl = QLabel("BRIEFING")
        self._content_title_lbl.setFont(QFont(UI_FONT, 8, QFont.Weight.Bold))
        self._content_title_lbl.setStyleSheet(
            f"color: {C.PRI}; background: transparent; letter-spacing: 1px;"
        )
        hdr.addWidget(self._content_title_lbl)
        hdr.addStretch()

        self._content_ts_lbl = QLabel("")
        self._content_ts_lbl.setFont(QFont(MONO_FONT, 7))
        self._content_ts_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        hdr.addWidget(self._content_ts_lbl)

        dismiss = QPushButton("DISMISS  ✕")
        dismiss.setFont(QFont(UI_FONT, 7))
        dismiss.setFixedHeight(18)
        dismiss.setCursor(Qt.CursorShape.PointingHandCursor)
        dismiss.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 9px; padding: 0 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        dismiss.clicked.connect(w.hide)
        hdr.addWidget(dismiss)
        lay.addLayout(hdr)

        # ── separator ─────────────────────────────────────────────────────────
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};"); lay.addWidget(sep)

        # ── text display ──────────────────────────────────────────────────────
        self._content_display = QTextEdit()
        self._content_display.setReadOnly(True)
        self._content_display.setFont(QFont(UI_FONT, 8))
        self._content_display.setMinimumHeight(60)
        self._content_display.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._content_display.setStyleSheet(f"""
            QTextEdit {{
                background: {C.DARK};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 12px;
                padding: 6px 8px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: {C.BG}; width: 6px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B}; border-radius: 3px; min-height: 16px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0; border: none;
            }}
        """)
        lay.addWidget(self._content_display)

        return w

    def _show_content(self, title: str, text: str):
        """Slot — runs on Qt main thread. Updates and shows the content panel."""
        import time as _time
        self._content_title_lbl.setText(title.upper()[:48])
        self._content_ts_lbl.setText(_time.strftime("%H:%M:%S"))
        self._content_display.setPlainText(text)
        self._content_display.moveCursor(
            self._content_display.textCursor().MoveOperation.Start
        )
        first_show = not self._content_panel.isVisible()
        self._content_panel.show()
        if first_show:
            total = self._center_split.height()
            self._center_split.setSizes([max(total - 220, 120), 220])

    def _on_node_clicked(self, node_id: str) -> None:
        """Slot for HudCanvas.node_clicked — runs on the Qt main thread
        (triggered by a real mouse event), so this can call _show_content
        directly instead of going through the cross-thread _content_sig."""
        node = next((n for n in HudCanvas._NODES if n["id"] == node_id), None)
        if node is None:
            return
        online = self.hud._node_status.get(node_id, True)
        lines = [node["desc"], "", f"Status: {'ONLINE' if online else 'OFFLINE'}"]
        if node["examples"]:
            lines += ["", "Example commands:"] + [f"  • {ex}" for ex in node["examples"]]
        self._show_content(node["label"], "\n".join(lines))

    def _build_footer(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(22)
        w.setStyleSheet(f"background: {C.DARK}; border-top: 1px solid {C.BORDER};")
        lay = QHBoxLayout(w); lay.setContentsMargins(14, 0, 14, 0)

        def _fl(txt, color=C.TEXT_MED):
            l = QLabel(txt); l.setFont(QFont(UI_FONT, 7))
            l.setStyleSheet(f"color: {color}; background: transparent;")
            return l

        lay.addWidget(_fl("[F4] Mute  ·  [F11] Fullscreen"))
        lay.addStretch()
        lay.addWidget(_fl("Platon Industries  ·  MARK XLVIII  ·  CLASSIFIED"))
        lay.addStretch()
        lay.addWidget(_fl("© STARK INDUSTRIES", C.PRI_DIM))
        return w

    def _on_file_selected(self, path: str):
        self._current_file = path
        p    = Path(path)
        cat  = _file_category(p)
        icon, _ = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size = _fmt_size(p.stat().st_size)
        self._file_hint.setText(f"{icon}  {p.name}  ·  {size}  ·  Tell JARVIS what to do with it")
        self._log.append_log(f"FILE: {p.name} ({size}) loaded")
        if self.on_text_command:
            msg = (
                f"[FILE_UPLOADED] path={path} | name={p.name} | "
                f"type={p.suffix.lstrip('.')} | size={size} | "
                f"Briefly tell the user you can see the file '{p.name}' "
                f"({size}) has been uploaded and ask what they'd like to do with it."
            )
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()

    def notify_phone_connected(self) -> None:
        if self._remote_overlay and self._remote_overlay.isVisible():
            self._remote_overlay.mark_connected()

    def _open_remote(self):
        if not self.on_remote_clicked:
            self._log.append_log("SYS: Dashboard not running — remote unavailable.")
            return
        result = self.on_remote_clicked()
        if not result:
            self._log.append_log("SYS: Could not generate remote key.")
            return
        url    = result[0]
        key    = result[1]
        auto   = result[2] if len(result) >= 3 else ""
        manual = result[3] if len(result) >= 4 else url
        if self._remote_overlay:
            self._remote_overlay._do_close()
        cw  = self.centralWidget()
        ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
        ov  = RemoteKeyOverlay(url, key, auto_login_url=auto, manual_url=manual,
                               expiry_secs=600, parent=cw)
        ov.set_new_key_callback(self.on_remote_clicked)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.closed.connect(lambda: setattr(self, '_remote_overlay', None))
        ov.show()
        self._remote_overlay = ov
        self._log.append_log(f"SYS: Remote key generated — manual: {manual or url}")

    def _do_interrupt(self):
        if self.on_interrupt:
            self.on_interrupt()

    def _toggle_mute(self):
        self._muted = not self._muted
        self.hud.muted = self._muted
        self._style_mute_btn()
        if self._muted:
            self._apply_state("MUTED")
            self._log.append_log("SYS: Microphone muted.")
        else:
            self._apply_state("LISTENING")
            self._log.append_log("SYS: Microphone active.")

    def _toggle_barge_in(self):
        self._barge_in = not self._barge_in
        self._style_barge_in_btn()
        if self._barge_in:
            self._log.append_log("SYS: Voice barge-in enabled — talk over JARVIS to interrupt it.")
        else:
            self._log.append_log("SYS: Voice barge-in disabled — use Esc/Interrupt button instead.")

    def _style_barge_in_btn(self):
        if self._barge_in:
            self._barge_in_btn.setText("Voice barge-in: on")
            self._barge_in_btn.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(159,232,201,0.10); color: {C.GREEN};
                    border: 1px solid rgba(159,232,201,0.35); border-radius: 20px;
                }}
                QPushButton:hover {{ background: rgba(159,232,201,0.16); }}
            """)
        else:
            self._barge_in_btn.setText("Voice barge-in: off")
            self._barge_in_btn.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(255,255,255,0.05); color: {C.TEXT_MED};
                    border: 1px solid rgba(255,255,255,0.15); border-radius: 20px;
                }}
                QPushButton:hover {{ background: rgba(255,255,255,0.09); }}
            """)

    def _style_mute_btn(self):
        if self._muted:
            self._mute_btn.setText("Microphone muted")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(255,157,157,0.10); color: {C.MUTED_C};
                    border: 1px solid rgba(255,157,157,0.35); border-radius: 20px;
                }}
                QPushButton:hover {{ background: rgba(255,157,157,0.16); }}
            """)
        else:
            self._mute_btn.setText("Microphone active")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: rgba(159,232,201,0.10); color: {C.GREEN};
                    border: 1px solid rgba(159,232,201,0.35); border-radius: 20px;
                }}
                QPushButton:hover {{ background: rgba(159,232,201,0.16); }}
            """)

    def _send(self):
        txt = self._input.text().strip()
        if not txt: return
        self._input.clear()
        self._log.append_log(f"You: {txt}")
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(txt,), daemon=True).start()

    def _apply_state(self, state: str):
        self.hud.state    = state
        self.hud.speaking = (state == "SPEAKING")

    def _check_config(self) -> bool:
        if not API_FILE.exists(): return False
        try:
            d = json.loads(API_FILE.read_text(encoding="utf-8"))
            return bool(d.get("gemini_api_key")) and bool(d.get("os_system"))
        except Exception:
            return False

    def _show_setup(self):
        ov = SetupOverlay(self.centralWidget())
        cw = self.centralWidget()
        ow, oh = 460, 470
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.done.connect(self._on_setup_done)
        ov.show()
        self._overlay = ov

    def _on_setup_done(self, key: str, claude_key: str, os_name: str):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        cfg = {"gemini_api_key": key, "claude_api_key": claude_key, "os_system": os_name}
        # Preserve any other settings already on disk (e.g. a Claude key added later
        # via prompt_reconfig) instead of clobbering them on every setup submission.
        if API_FILE.exists():
            try:
                existing = json.loads(API_FILE.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    existing.update(cfg)
                    cfg = existing
            except Exception:
                pass
        API_FILE.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
        try:
            from core.runtime_config import invalidate_config_cache
            invalidate_config_cache()
        except Exception:
            pass
        self._ready = True
        if self._overlay:
            self._overlay.hide()
            self._overlay = None
        self._apply_state("LISTENING")
        self._log.append_log(f"SYS: Initialised. OS={os_name.upper()}. JARVIS online.")

class _RootShim:
    def __init__(self, app: QApplication):
        self._app = app
    def mainloop(self):
        self._app.exec()
    def protocol(self, *_):
        pass


class JarvisUI:
    def __init__(self, face_path: str, size=None):
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setStyle("Fusion")
        _load_bundled_fonts()

        self._splash = ReactorSplash()
        self._splash.show()

        self._win = MainWindow(face_path)
        self._win.hide()
        QTimer.singleShot(2600, self._finish_splash)

        self.root = _RootShim(self._app)

    def _finish_splash(self):
        if self._splash:
            self._splash.hide()
            self._splash.deleteLater()
            self._splash = None
        self._win.show()

    @property
    def muted(self) -> bool:
        return self._win._muted

    @muted.setter
    def muted(self, v: bool):
        if v != self._win._muted:
            self._win._toggle_mute()

    @property
    def barge_in_enabled(self) -> bool:
        return self._win._barge_in

    @barge_in_enabled.setter
    def barge_in_enabled(self, v: bool):
        if v != self._win._barge_in:
            self._win._toggle_barge_in()

    @property
    def current_file(self) -> str | None:
        return self._win._drop_zone.current_file()

    @property
    def on_text_command(self):
        return self._win.on_text_command

    @on_text_command.setter
    def on_text_command(self, cb):
        self._win.on_text_command = cb

    @property
    def on_remote_clicked(self):
        return self._win.on_remote_clicked

    @on_remote_clicked.setter
    def on_remote_clicked(self, cb):
        self._win.on_remote_clicked = cb

    @property
    def on_interrupt(self):
        return self._win.on_interrupt

    @on_interrupt.setter
    def on_interrupt(self, cb):
        self._win.on_interrupt = cb

    def notify_phone_connected(self) -> None:
        self._win.notify_phone_connected()

    def set_state(self, state: str):
        self._win._state_sig.emit(state)

    def write_log(self, text: str):
        self._win._log_sig.emit(text)

    def wait_for_api_key(self):
        while not self._win._ready:
            time.sleep(0.1)

    def show_content(self, title: str, text: str):
        """Thread-safe: display content in the panel below the HUD."""
        self._win._content_sig.emit(title[:48], text[:4000])

    def set_subsystem_status(self, status: dict[str, bool]) -> None:
        """Thread-safe: update the HUD's subsystem constellation (real
        component status — Fast Path ready, Telegram connected, etc.)."""
        self._win._status_sig.emit(status)

    def prompt_reconfig(self):
        """Thread-safe: show the API key setup overlay (e.g. after an auth error)."""
        self._win._ready = False
        self._win._reconfig_sig.emit()

    def show_camera_frame(self, img_bytes: bytes):
        """Thread-safe: show a webcam frame in the small overlay (screen captures)."""
        self._win._camera_sig.emit(img_bytes)

    def start_camera_stream(self) -> None:
        """Thread-safe: start live camera feed in the full HUD area."""
        self._win.start_camera_stream()

    def stop_camera_stream(self) -> None:
        """Thread-safe: stop the live camera feed."""
        self._win.stop_camera_stream()

    def start_speaking(self):
        self.set_state("SPEAKING")

    def stop_speaking(self):
        if not self.muted:
            self.set_state("LISTENING")