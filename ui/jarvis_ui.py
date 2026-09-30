"""
_RootShim/JarvisUI: the public facade main.py talks to (`from ui import
JarvisUI`), plus the Tk-style `.root.mainloop()` shim main.py's runner()
calls.

Split out of ui.py (Stage 2 module split, see REWORK_PLAN.md) with no
behavior changes.
"""
from __future__ import annotations

import sys
import time

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from core.path_utils import resource_path
from ui.fonts import _load_bundled_fonts
from ui.main_window import MainWindow
from ui.compact_bar import CompactBar
from core.sound_effects import play_startup_sequence


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
        self._app.setQuitOnLastWindowClosed(False)  # stay alive with only the tray icon visible
        _load_bundled_fonts()

        # MainWindow (the full dashboard-style HUD) is still built eagerly
        # but hidden -- every JarvisUI facade property below reaches
        # directly into self._win, so keeping construction eager avoids
        # adding "does self._win exist yet" guards everywhere. It just no
        # longer auto-shows after the splash (see _finish_splash) -- the
        # app now starts silent, ambient, wake-word-gated (main.py's
        # _require_wake_word), with only the compact bar appearing while
        # actually in use and the tray icon as the way back to this full
        # window.
        self._win = MainWindow(face_path)
        self._win.hide()
        self._win_shown_once = False

        self._bar = CompactBar()

        self._tray = self._build_tray_icon()

        # The full HUD stays hidden until the user opens it from the tray
        # icon -- UNLESS setup isn't done yet (no/invalid API key: see
        # MainWindow._check_config()), in which case the setup overlay
        # (a child widget of the hidden window) would otherwise be built
        # but never actually seen, silently stalling first-run setup.
        if not self._win._ready:
            self._show_main_window()
        # Audio-only confirmation JARVIS is up and listening for the wake
        # word -- no visible HUD/splash needed for that part.
        play_startup_sequence()

        self.root = _RootShim(self._app)

    def _build_tray_icon(self) -> QSystemTrayIcon:
        ico_path = resource_path("config/jarvis.ico")
        icon = QIcon(str(ico_path)) if ico_path.exists() else self._win.windowIcon()
        tray = QSystemTrayIcon(icon, self._app)
        tray.setToolTip("J.A.R.V.I.S — MARK XLVIII")
        menu = QMenu()
        show_action = menu.addAction("Show JARVIS")
        show_action.triggered.connect(self._show_main_window)
        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(self._app.quit)
        tray.setContextMenu(menu)
        tray.activated.connect(
            lambda reason: self._show_main_window()
            if reason == QSystemTrayIcon.ActivationReason.Trigger
            else None
        )
        tray.show()
        return tray

    def _show_main_window(self) -> None:
        if not self._win_shown_once:
            self._win_shown_once = True
            self._win.hud.begin_startup_sequence()
        self._win.show()
        self._win.raise_()
        self._win.activateWindow()

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
        self._bar._state_sig.emit(state)

    def show_compact_bar(self) -> None:
        """Thread-safe: reveal the small always-on-top bar (voice/typed/
        dashboard/Telegram wake -- see main.py's _require_wake_word and
        core/fast_path.py's wake-word-triggered branch)."""
        self._bar._show_sig.emit()

    def hide_compact_bar(self) -> None:
        """Thread-safe: shrink the bar back to invisible (idle-close, see
        core/idle_watchdog.py, or a pure local command that never needed
        Gemini, see core/fast_path.py's handle_local_text())."""
        self._bar._hide_sig.emit()

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

    def set_security_scan(self, active: bool, message: str) -> None:
        self._win._scan_sig.emit(active, message)

    def set_power_mode(self, active: bool) -> None:
        self._win._power_sig.emit(active)

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
