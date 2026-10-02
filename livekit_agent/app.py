"""System tray wrapper that turns the voice agent into a local desktop app.

Spawns two child processes:
  - `worker.py console` -- LiveKit Agents' local-mic/speaker mode (no LiveKit
    Cloud room needed), plus the F10 wake hotkey / auto-sleep (sleep_wake.py).
  - `hud_bar.py` -- the floating status pill (compact_bar.py, ported from the
    original Jarvis desktop app), reading worker.py's state via hud_bridge.py.

Exposes a tray icon with Restart / Open logs / Exit. Install autostart
separately with `install_autostart.py`.

Run directly for a visible console window (debugging):
    python app.py
Or via pythonw.exe for a silent background app (what autostart uses):
    pythonw.exe app.py
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pystray
from PIL import Image

import config
import notify

BASE_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable
APP_LOG_PATH = config.LOGS_DIR / "app.log"
BAR_LOG_PATH = config.LOGS_DIR / "hud_bar.log"
PHONE_LOG_PATH = config.LOGS_DIR / "phone_worker.log"
TELEGRAM_LOG_PATH = config.LOGS_DIR / "telegram_bridge.log"
MONITOR_LOG_PATH = config.LOGS_DIR / "proactive_monitor.log"
CHAT_MEMORY_LOG_PATH = config.LOGS_DIR / "chat_memory.log"
SUPERVISOR_LOG_PATH = config.LOGS_DIR / "supervisor.log"
PANEL_LOG_PATH = config.LOGS_DIR / "panel.log"
PANEL_PORT = 8765
ICON_PATH = BASE_DIR / "assets" / "jarvis.ico"


def _rotate_log(path: Path) -> None:
    """Keep one previous generation (<name>.1) once a log passes LOG_MAX_BYTES."""
    try:
        if path.exists() and path.stat().st_size > config.LOG_MAX_BYTES:
            os.replace(path, path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass  # held open elsewhere -- try again on the next start


_rotate_log(SUPERVISOR_LOG_PATH)
logging.basicConfig(
    filename=SUPERVISOR_LOG_PATH, level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("jarvis-voice-agent.supervisor")

_WATCHDOG_INTERVAL_S = 30.0
# Backs off a process that's crash-looping instead of hammering it -- 5
# restarts within 5 minutes means something is actually broken (no network,
# an expired key), not a one-off blip. It isn't given up on for good,
# though: after _COOLDOWN_S it gets a fresh set of attempts, because the
# usual causes (network back, balance topped up) fix themselves while
# nobody is at the PC. The owner is told on Telegram either way.
_MAX_RESTARTS_PER_WINDOW = 5
_RESTART_WINDOW_S = 300.0
_COOLDOWN_S = 1800.0


def _worker_args() -> list[str]:
    args = [str(BASE_DIR / "worker.py"), "console", "--no-text"]
    if config.AUDIO_INPUT_DEVICE:
        args += ["--input-device", config.AUDIO_INPUT_DEVICE]
    if config.AUDIO_OUTPUT_DEVICE:
        args += ["--output-device", config.AUDIO_OUTPUT_DEVICE]
    return args


class _ManagedProcess:
    """A child process with its own log file, restartable/stoppable idempotently."""

    def __init__(self, name: str, args: list[str], log_path: Path, enabled=None) -> None:
        self.name = name
        self._args = args
        self._log_path = log_path
        # Optional components (phone, Telegram) only run once their settings
        # exist -- checked against the *current* .env each time, so filling
        # them in from the control panel and hitting "Перезапустить" is enough.
        self._enabled = enabled
        self._proc: subprocess.Popen | None = None
        self._log_file = None
        self._restart_times: list[float] = []
        self._cooldown_until = 0.0

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def is_enabled(self) -> bool:
        try:
            return self._enabled is None or bool(self._enabled())
        except Exception:
            return False

    def restart_if_dead(self) -> None:
        """Watchdog hook: brings a crashed process back on its own, with a
        backoff so a persistently broken process doesn't get hammered with
        restarts forever."""
        if not self.is_enabled() or self.is_alive():
            return
        now = time.time()
        if now < self._cooldown_until:
            return
        self._restart_times = [t for t in self._restart_times if now - t < _RESTART_WINDOW_S]
        if len(self._restart_times) >= _MAX_RESTARTS_PER_WINDOW:
            self._cooldown_until = now + _COOLDOWN_S
            self._restart_times = []
            logger.warning(
                "%s keeps crashing (%d restarts in %.0fs) -- pausing restarts for %.0f min, check %s",
                self.name, _MAX_RESTARTS_PER_WINDOW, _RESTART_WINDOW_S, _COOLDOWN_S / 60, self._log_path,
            )
            notify.notify_owner(
                f"⚠️ Процесс «{self.name}» падает раз за разом ({_MAX_RESTARTS_PER_WINDOW} раз за "
                f"{_RESTART_WINDOW_S / 60:.0f} мин). Попробую снова через {_COOLDOWN_S / 60:.0f} мин. "
                f"Частые причины: нет интернета, кончился баланс API, неверный ключ. Лог: {self._log_path.name}",
                kind="supervisor",
            )
            return
        self._restart_times.append(now)
        logger.info("%s died -- restarting it (%d/%d this window)", self.name, len(self._restart_times), _MAX_RESTARTS_PER_WINDOW)
        self.start()

    def start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        if not self.is_enabled():
            logger.info("%s is not configured yet -- skipping", self.name)
            return
        _rotate_log(self._log_path)
        self._log_file = open(self._log_path, "a", encoding="utf-8")
        env = dict(os.environ)
        # worker.py console prints a startup banner via `rich`; force UTF-8 so
        # it doesn't crash under the cp1251 codepage some Windows locales use.
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self._proc = subprocess.Popen(
            [PYTHON, *self._args],
            cwd=str(BASE_DIR),
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=creationflags,
        )

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None


def _fresh_env() -> dict[str, str | None]:
    from dotenv import dotenv_values

    return dotenv_values(config.ENV_FILE)


def _core_configured() -> bool:
    env = _fresh_env()
    # LLM_PROVIDER=claude_code talks through the logged-in `claude` CLI, no API key.
    has_brain = env.get("ANTHROPIC_API_KEY") or env.get("LLM_PROVIDER") == "claude_code"
    return bool(has_brain and env.get("DEEPGRAM_API_KEY"))


def _livekit_configured() -> bool:
    env = _fresh_env()
    return all(env.get(k) for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"))


def _telegram_configured() -> bool:
    env = _fresh_env()
    logged_in = Path(config.TELEGRAM_SESSION_PATH + ".session").exists()
    return bool(env.get("TELEGRAM_API_ID") and env.get("TELEGRAM_API_HASH") and logged_in)


_worker = _ManagedProcess("worker(console)", _worker_args(), APP_LOG_PATH, enabled=_core_configured)
_bar = _ManagedProcess("hud_bar", [str(BASE_DIR / "hud_bar.py")], BAR_LOG_PATH, enabled=_core_configured)
# "start" is the production worker mode that actually registers with LiveKit
# Cloud and accepts room dispatch jobs -- including the ones the SIP dispatch
# rule creates for inbound phone calls. "console" (the process above) never
# goes through room dispatch at all, so phone calls need this running too.
_phone_worker = _ManagedProcess("worker(start/phone)", [str(BASE_DIR / "worker.py"), "start"], PHONE_LOG_PATH, enabled=_livekit_configured)
_telegram_bridge = _ManagedProcess("telegram_bridge", [str(BASE_DIR / "telegram_bridge.py")], TELEGRAM_LOG_PATH, enabled=_telegram_configured)
_monitor = _ManagedProcess("proactive_monitor", [str(BASE_DIR / "proactive_monitor.py")], MONITOR_LOG_PATH, enabled=_telegram_configured)
# The control panel (settings, capabilities, voices) stays up for as long as
# the tray app does, so "Открыть панель" in the tray always has something to open.
_panel = _ManagedProcess("panel", [str(BASE_DIR / "panel.py"), "--no-browser", "--port", str(PANEL_PORT)], PANEL_LOG_PATH)
# Shared memory of chats and conversations: archives Telegram (when set up)
# and digests the journal -- the digest is useful with voice alone, so it
# runs whenever the core does.
_chat_memory = _ManagedProcess("chat_memory", [str(BASE_DIR / "chat_memory.py")], CHAT_MEMORY_LOG_PATH, enabled=_core_configured)
_ALL_PROCESSES = [_worker, _bar, _phone_worker, _telegram_bridge, _monitor, _chat_memory, _panel]


def _watchdog_loop() -> None:
    while True:
        time.sleep(_WATCHDOG_INTERVAL_S)
        for proc in _ALL_PROCESSES:
            try:
                proc.restart_if_dead()
            except Exception:
                logger.exception("watchdog check failed for %s", proc.name)


def _make_icon_image() -> Image.Image:
    # Reuses the tray icon from the original Jarvis desktop app for visual
    # continuity between the two projects.
    return Image.open(ICON_PATH)


def _start_all() -> None:
    _worker.start()
    _bar.start()
    _phone_worker.start()
    _telegram_bridge.start()
    _monitor.start()
    _chat_memory.start()
    _panel.start()


def _stop_all() -> None:
    _worker.stop()
    _bar.stop()
    _phone_worker.stop()
    _telegram_bridge.stop()
    _monitor.stop()
    _chat_memory.stop()
    _panel.stop()


def _on_restart(icon: pystray.Icon, item: pystray.MenuItem) -> None:
    _stop_all()
    _start_all()


def _panel_listening() -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", PANEL_PORT)) == 0


def _open_panel_when_ready() -> None:
    """The panel is a child process that needs a second or two to start;
    opening the browser before it listens would show "connection refused"."""
    import webbrowser

    deadline = time.time() + 20
    while time.time() < deadline and not _panel_listening():
        time.sleep(0.4)
    token_file = config.DATA_DIR / "panel_token.txt"
    token = token_file.read_text(encoding="utf-8").strip() if token_file.exists() else ""
    webbrowser.open(f"http://127.0.0.1:{PANEL_PORT}/?t={token}")


def _on_open_panel(icon: pystray.Icon | None, item: pystray.MenuItem | None) -> None:
    threading.Thread(target=_open_panel_when_ready, daemon=True, name="open-panel").start()


_instance_mutex = None


def _already_running() -> bool:
    """True if another tray app is already up (double-click, autostart plus a
    shortcut, ...). Two copies would double-listen to the microphone."""
    global _instance_mutex
    if sys.platform != "win32":
        return False
    import ctypes

    _instance_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\JarvisAI.TrayApp")
    return ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


def _on_open_logs(icon: pystray.Icon, item: pystray.MenuItem) -> None:
    if sys.platform == "win32":
        os.startfile(APP_LOG_PATH)  # type: ignore[attr-defined]


def _on_exit(icon: pystray.Icon, item: pystray.MenuItem) -> None:
    _stop_all()
    icon.stop()


def main() -> None:
    if _already_running():
        _on_open_panel(None, None)
        time.sleep(3)  # let the panel-opener thread finish before the process exits
        return
    _start_all()
    if not _core_configured():
        # First run: nothing useful to do without the Claude/Deepgram keys, so
        # take the user straight to the panel where they enter them.
        _on_open_panel(None, None)
    threading.Thread(target=_watchdog_loop, daemon=True, name="jarvis-watchdog").start()

    icon = pystray.Icon(
        "jarvis-voice-agent",
        _make_icon_image(),
        "Джарвис (голосовой агент)",
        menu=pystray.Menu(
            pystray.MenuItem("Перезапустить", _on_restart, default=True),
            pystray.MenuItem("Открыть панель", _on_open_panel),
            pystray.MenuItem("Открыть логи", _on_open_logs),
            pystray.MenuItem("Выход", _on_exit),
        ),
    )
    try:
        icon.run()  # blocks until _on_exit calls icon.stop()
    finally:
        _stop_all()


if __name__ == "__main__":
    main()
