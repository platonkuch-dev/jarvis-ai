from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def resource_path(relative_path: str) -> Path:
    """Return a resource path for both regular runs and PyInstaller builds."""
    if getattr(sys, "frozen", False):
        base_path = Path(sys._MEIPASS)
    else:
        base_path = Path(__file__).resolve().parent.parent

    return base_path / relative_path


def get_user_data_dir() -> Path:
    """Return a writable directory for user settings, memory and similar data."""
    appdata = os.getenv("APPDATA")
    base_path = Path(appdata) if appdata else Path.home()
    user_dir = base_path / "Jarvis"
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


def _migrate_if_needed(target_path: Path, legacy_relative_path: str) -> Path:
    if target_path.exists():
        return target_path

    legacy_path = resource_path(legacy_relative_path)
    if legacy_path.exists():
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy_path, target_path)
        except Exception:
            pass
    return target_path


def get_config_path() -> Path:
    config_path = get_user_data_dir() / "api_keys.json"
    return _migrate_if_needed(config_path, "config/api_keys.json")


def get_memory_path() -> Path:
    memory_path = get_user_data_dir() / "long_term.json"
    return _migrate_if_needed(memory_path, "memory/long_term.json")
