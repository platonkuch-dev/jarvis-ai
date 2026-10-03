"""The HUD panel: Jarvis's face + status + subtitles + the day plan as one
full-screen page on a monitor (dayplan/index.html).

A tiny localhost HTTP server feeds it:
  /            the page (dayplan/index.html)
  /data.json   the day plan (dayplan_data.collect())
  /events      Server-Sent Events, ~30 per second: status, voice level,
               sibilance, screen-watch flag, and the conversation lines and
               the live Claude Code session (code_feed.py) when they change --
               so the face breathes, thinks and talks live, and coding shows
               up here instead of in a console.

Normally hud_bar.py hosts it (it already owns the voice-level feed from
lipsync_bridge and polls hud_bridge); ensure_server() starts a fallback copy
in the calling process when nothing is listening yet, so show_day_plan works
even without the HUD. Only 127.0.0.1, only these fixed routes, and requests
with a foreign Host header are refused (DNS rebinding).

open_panel()/close_panel() place a Chrome/Edge app window (own profile, full
screen) on a monitor's real pixel rectangle -- see screens.py.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

import config
from atomic_io import atomic_write_text

logger = logging.getLogger("jarvis-voice-agent.hud_panel")

PORT = config.HUD_PANEL_PORT
PAGE = config.BASE_DIR / "dayplan" / "index.html"
PROFILE_DIR = config.DATA_DIR / "dayplan_browser"
URL = f"http://127.0.0.1:{PORT}/"
STATE_FILE = config.DATA_DIR / "hud_panel_state.json"
_ACTIVITY_FILE = config.DATA_DIR / "tool_activity.json"     # tools/_logging.py: the tool running now
_TICK_S = 1 / 30
_SLEEP_TICK_S = 0.2


def _read_panel_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_panel_state(**changes) -> None:
    state = _read_panel_state()
    state.update(changes, at=time.time())
    atomic_write_text(STATE_FILE, json.dumps(state))


def set_plan(visible: bool) -> None:
    """Show/hide the day plan inside the open panel (it animates in/out)."""
    _write_panel_state(plan=bool(visible))


def plan_visible() -> bool:
    return bool(_read_panel_state().get("plan"))


def set_plan_day(offset: int) -> None:
    """Which day the plan shows: 0 = today, 1 = tomorrow, -1 = yesterday...
    Picked up by the open page's /events feed, same as plan/face."""
    _write_panel_state(plan_day=int(offset))


def plan_day() -> int:
    return int(_read_panel_state().get("plan_day") or 0)


def set_face(visible: bool) -> None:
    """Jarvis's main form is the 3D neuron; this opens (or closes) his face in its place."""
    _write_panel_state(face=bool(visible))


def face_visible() -> bool:
    return bool(_read_panel_state().get("face"))


_net_prev: dict = {}


def system_stats() -> dict:
    """PC telemetry for the HUD (psutil only, no LLM): CPU, RAM, disk, network rate, uptime."""
    import psutil

    now = time.time()
    net = psutil.net_io_counters()
    prev = _net_prev.get("v")
    rx = tx = 0.0
    if prev is not None and now > prev[0]:
        dt = now - prev[0]
        rx, tx = (net.bytes_recv - prev[1]) / dt, (net.bytes_sent - prev[2]) / dt
    _net_prev["v"] = (now, net.bytes_recv, net.bytes_sent)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("C:\\" if os.name == "nt" else "/")
    battery = psutil.sensors_battery()
    return {"cpu": psutil.cpu_percent(interval=None), "cores": psutil.cpu_count(),
            "ram": mem.percent, "ram_used": round(mem.used / 2**30, 1), "ram_total": round(mem.total / 2**30, 1),
            "disk": disk.percent, "disk_free": round(disk.free / 2**30), "rx": rx, "tx": tx,
            "uptime": int(now - psutil.boot_time()), "procs": len(psutil.pids()),
            "battery": round(battery.percent) if battery else None}


class _Feed:
    """What /events streams. level_fn returns (level, sibilance) of the voice."""

    def __init__(self, level_fn: Callable[[], tuple[float, float]] | None = None) -> None:
        self.level_fn = level_fn
        self.clients = 0
        self._lock = threading.Lock()

    def add(self, delta: int) -> None:
        with self._lock:
            self.clients += delta


def _handler(feed: _Feed):
    allowed_hosts = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:   # keep the HUD log quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:   # noqa: N802
            """/control {"mic": bool} / {"plan": bool} / {"face": bool} / {"code_stop": true} -- the panel's buttons.
            The custom X-Jarvis header can't be sent cross-site without a CORS
            preflight, which this server never approves: only the panel's own
            page can press these buttons."""
            if self.headers.get("Host", "") not in allowed_hosts or self.headers.get("X-Jarvis") != "1"                     or self.path.split("?", 1)[0] != "/control":
                self._send(403, b"forbidden", "text/plain")
                return
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 4096)
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, OSError):
                self._send(400, b"bad request", "text/plain")
                return
            import mic_control

            if "mic" in body:
                mic_control.set_muted(not bool(body["mic"]))
                logger.info("panel button: microphone %s", "on" if body["mic"] else "off")
            if "plan" in body:
                set_plan(bool(body["plan"]))
            if "face" in body:
                set_face(bool(body["face"]))
            if body.get("code_stop"):
                import code_feed

                code_feed.request_stop()
                logger.info("panel button: stop Claude Code")
            self._send(200, b'{"ok": true}', "application/json")

        def do_GET(self) -> None:   # noqa: N802
            if self.headers.get("Host", "") not in allowed_hosts:
                self._send(403, b"forbidden", "text/plain")
                return
            path, _, query = self.path.partition("?")
            if path in ("/", "/index.html"):
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif path == "/sys.json":
                self._send(200, json.dumps(system_stats()).encode("utf-8"), "application/json")
            elif path == "/ping":
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
            elif path == "/plan.html":
                self._send(200, (PAGE.parent / "plan.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/data.json":
                import urllib.parse

                import dayplan_data

                try:
                    offset = int(urllib.parse.parse_qs(query).get("day", ["0"])[0])
                except ValueError:
                    offset = 0
                offset = max(-31, min(31, offset))
                body = json.dumps(dayplan_data.collect(offset), ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            elif path == "/events":
                self._stream()
            else:
                self._send(404, b"not found", "text/plain")

        def _stream(self) -> None:
            import hud_bridge
            import screen_watch_bridge

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            feed.add(1)
            import code_feed
            import mic_control

            last_lines, last_state_read, state, watching, plan, muted, face, plan_day = \
                None, 0.0, {}, False, False, False, False, 0
            code_seen = -1.0
            act_seen = -1.0
            try:
                while True:
                    now = time.monotonic()
                    if now - last_state_read > 0.15:
                        state = hud_bridge.read_state()
                        try:
                            watching = bool(screen_watch_bridge.read_state().get("watching"))
                        except Exception:
                            watching = False
                        panel = _read_panel_state()
                        plan, face = bool(panel.get("plan")), bool(panel.get("face"))
                        plan_day = int(panel.get("plan_day") or 0)
                        muted = mic_control.is_muted()
                        last_state_read = now
                    level, sib = feed.level_fn() if feed.level_fn else (0.0, 0.0)
                    msg = {"s": state.get("status", "idle"), "l": round(float(level), 3),
                           "b": round(float(sib), 3), "w": watching, "p": plan, "f": face, "pd": plan_day,
                           "m": muted, "u": state.get("updated_at", 0.0)}
                    lines = state.get("lines") or []
                    if lines != last_lines:
                        msg["lines"] = lines[-4:]
                        last_lines = lines
                    try:
                        act_mtime = _ACTIVITY_FILE.stat().st_mtime
                    except OSError:
                        act_mtime = 0.0
                    if act_mtime != act_seen:
                        act_seen = act_mtime
                        try:
                            msg["t"] = json.loads(_ACTIVITY_FILE.read_text(encoding="utf-8"))
                        except (OSError, ValueError):
                            pass
                    code_mtime = code_feed.mtime()
                    if code_mtime != code_seen:
                        msg["code"] = code_feed.read()
                        code_seen = code_mtime
                    self.wfile.write(b"data: " + json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n\n")
                    self.wfile.flush()
                    # asleep the face only breathes: 5 updates a second are plenty
                    time.sleep(_SLEEP_TICK_S if msg["s"] == "sleeping" else _TICK_S)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                feed.add(-1)

    return Handler


_server: ThreadingHTTPServer | None = None
feed = _Feed()


def serve(level_fn: Callable[[], tuple[float, float]] | None = None) -> bool:
    """Start the panel server in this process (daemon thread). False if the
    port is already taken -- then someone else is serving it, which is fine."""
    global _server
    if level_fn is not None:
        feed.level_fn = level_fn
    if _server is not None:
        return True
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), _handler(feed))
    except OSError:
        return False
    server.daemon_threads = True
    _server = server
    threading.Thread(target=server.serve_forever, name="hud-panel-server", daemon=True).start()
    logger.info("HUD panel server on %s", URL)
    return True


def server_up() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.3):
            return True
    except OSError:
        return False


def ensure_server() -> None:
    if not server_up():
        serve()


def browser_exe() -> str | None:
    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    found = next((str(p) for p in candidates if p.is_file()), None)
    return found or shutil.which("chrome") or shutil.which("msedge")


def is_open() -> bool:
    try:
        import psutil

        marker = str(PROFILE_DIR).lower()
        return any(marker in " ".join(p.info.get("cmdline") or []).lower()
                   for p in psutil.process_iter(["cmdline"]))
    except Exception:
        return False


def close_panel() -> bool:
    try:
        import psutil
    except ImportError:
        return False
    marker, killed = str(PROFILE_DIR).lower(), False
    for p in psutil.process_iter(["cmdline"]):
        try:
            if marker in " ".join(p.info.get("cmdline") or []).lower():
                p.kill()
                killed = True
        except Exception:
            continue
    return killed


def open_panel(monitor: int) -> int:
    """Opens (or re-opens) the panel full screen on `monitor`. -> the monitor used."""
    import screens

    mons = screens.monitors()
    if monitor > len(mons) or monitor < 1:
        monitor = 1
    mon = mons[monitor - 1]
    exe = browser_exe()
    if exe is None:
        raise RuntimeError("Не нашёл Chrome или Edge для панели.")
    ensure_server()
    close_panel()
    # Chrome takes these in physical pixels of the virtual screen -- exactly
    # screens.py's monitor rect, verified on a mixed-DPI (150 % + 100 %) setup.
    subprocess.Popen([
        exe, f"--app={URL}", f"--user-data-dir={PROFILE_DIR}",
        f"--window-position={mon.left},{mon.top}", f"--window-size={mon.width},{mon.height}",
        # --kiosk: full screen without Chrome's "hold Esc to exit" banner
        "--kiosk", "--no-first-run", "--no-default-browser-check",
        "--disable-features=Translate", "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
    ], creationflags=config.NO_WINDOW)
    return monitor
