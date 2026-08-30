"""
Bundled typography: Sora (UI labels, headings, prose) + IBM Plex Mono
(data: clocks, metrics, codes, log timestamps). Loaded from config/fonts/
once a QApplication exists (see _load_bundled_fonts, called from
JarvisUI.__init__); UI_FONT/MONO_FONT are updated in place, with
system-font fallbacks if the bundled files can't be loaded.

Split out of ui.py (Stage 2 module split, see REWORK_PLAN.md) with no
behavior changes.

IMPORTANT for callers: UI_FONT/MONO_FONT are mutated in place by
_load_bundled_fonts() *after* this module is first imported (it runs from
JarvisUI.__init__, once a QApplication exists). Do NOT
`from ui.fonts import UI_FONT` — that copies the pre-load fallback value
and never sees the update. Import the module instead
(`from ui import fonts`) and read `fonts.UI_FONT` / `fonts.MONO_FONT` at
the point of use, exactly as every widget in ui/ does.
"""
from __future__ import annotations

from PyQt6.QtGui import QFontDatabase

from core.path_utils import resource_path
from ui.consts import _OS

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
