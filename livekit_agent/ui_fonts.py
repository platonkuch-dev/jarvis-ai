"""
Bundled typography: Sora (UI labels) + IBM Plex Mono (data/log text).

Ported from the original Jarvis desktop app's ui/fonts.py -- same fonts,
same fallback behavior -- adapted only to load from this project's own
assets/fonts/ instead of core.path_utils.resource_path.

IMPORTANT for callers, unchanged from the original: UI_FONT/MONO_FONT are
mutated in place by load_bundled_fonts() *after* a QApplication exists.
Import the module (`import ui_fonts`) and read `ui_fonts.UI_FONT` at the
point of use -- `from ui_fonts import UI_FONT` would copy the pre-load
fallback value and never see the update.
"""
from __future__ import annotations

import platform
from pathlib import Path

from PyQt6.QtGui import QFontDatabase

_OS = platform.system()
_FONTS_DIR = Path(__file__).resolve().parent / "assets" / "fonts"

UI_FONT = "Segoe UI" if _OS == "Windows" else ("SF Pro Text" if _OS == "Darwin" else "Noto Sans")
MONO_FONT = "Consolas" if _OS == "Windows" else ("Menlo" if _OS == "Darwin" else "monospace")


def load_bundled_fonts() -> None:
    """Register the bundled Sora / IBM Plex Mono font files. Must run after
    a QApplication exists. Falls back to system fonts silently on failure."""
    global UI_FONT, MONO_FONT
    try:
        ui_id = QFontDatabase.addApplicationFont(str(_FONTS_DIR / "Sora-Variable.ttf"))
        if ui_id != -1 and QFontDatabase.applicationFontFamilies(ui_id):
            UI_FONT = "Sora"
        for fname in ("IBMPlexMono-Regular.ttf", "IBMPlexMono-Medium.ttf", "IBMPlexMono-Bold.ttf"):
            mono_id = QFontDatabase.addApplicationFont(str(_FONTS_DIR / fname))
            if mono_id != -1 and QFontDatabase.applicationFontFamilies(mono_id):
                MONO_FONT = "IBM Plex Mono"
    except Exception as e:
        print(f"[Fonts] Bundled font load failed, using system fonts: {e}")
