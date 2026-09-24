"""
Minimal Glass color palette and QColor helpers.

Ported verbatim from the original Jarvis desktop app's ui/colors.py, so the
new compact bar (compact_bar.py) matches that project's design exactly
rather than reinventing one.
"""
from __future__ import annotations

from PyQt6.QtGui import QColor


class C:
    """Minimal Glass palette — one soft accent, calm neutrals, semantic
    warning/critical colors kept separate from the accent hue."""
    BG        = "#0b1016"
    PANEL     = "#111820"
    PANEL2    = "#18212b"
    BORDER    = "#25313d"
    BORDER_B  = "#334454"
    BORDER_A  = "#2b3946"
    PRI       = "#84dfff"
    PRI_DIM   = "#5ba4bf"
    PRI_GHO   = "#163541"
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
