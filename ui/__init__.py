"""
ui/ -- PyQt6 HUD, split out of the former monolithic ui.py (Stage 2
module split, see REWORK_PLAN.md). Re-exports the same public surface
main.py (and nothing else in the project, per grep) ever imported from
`ui`: `from ui import JarvisUI`.
"""
from __future__ import annotations

from ui.jarvis_ui import JarvisUI

__all__ = ["JarvisUI"]
