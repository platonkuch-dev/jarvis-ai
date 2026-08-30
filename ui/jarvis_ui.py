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

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from ui.fonts import _load_bundled_fonts
from ui.reactor_splash import ReactorSplash
from ui.main_window import MainWindow


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
