"""
Shared, immutable top-of-file constants used across the ui/ package.

Split out of ui.py (Stage 2 module split, see REWORK_PLAN.md) with no
behavior changes.
"""
from __future__ import annotations

import platform
import subprocess
from pathlib import Path

from core.path_utils import get_config_path

if platform.system() == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}

BASE_DIR   = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
API_FILE   = get_config_path()

_DEFAULT_W, _DEFAULT_H = 980, 700
_MIN_W,     _MIN_H     = 820, 580
_LEFT_W  = 148
_RIGHT_W = 340

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"
