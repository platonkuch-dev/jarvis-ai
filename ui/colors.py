"""
Minimal Glass color palette and QColor helpers.

Split out of ui.py (Stage 2 module split, see REWORK_PLAN.md) with no
behavior changes.
"""
from __future__ import annotations

from PyQt6.QtGui import QColor


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
