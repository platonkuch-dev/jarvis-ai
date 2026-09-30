"""Microphone mute switch shared between processes.

The HUD panel's mic button (hud_panel.py, in the HUD process), the Telegram
chat or a voice command (tools/voice_control.py) write one tiny JSON file;
the voice worker (sleep_wake.py) polls it and turns its audio input off/on.
Muted means deaf: no speech and no "Hey Jarvis" either -- F9 (MIC_HOTKEY,
it toggles), the panel button, F10 or "включи микрофон" in Telegram turn it back on.
"""

from __future__ import annotations

import json
import time

import config

STATE_FILE = config.DATA_DIR / "mic_state.json"


def set_muted(muted: bool) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"muted": bool(muted), "at": time.time()}), encoding="utf-8")
    tmp.replace(STATE_FILE)


def is_muted() -> bool:
    try:
        return bool(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("muted"))
    except (OSError, ValueError):
        return False
