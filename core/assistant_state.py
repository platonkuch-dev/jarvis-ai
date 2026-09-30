"""Persistent local state for user-managed JARVIS features."""
from __future__ import annotations

import json
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from core.path_utils import get_user_data_dir


STATE_PATH = get_user_data_dir() / "assistant_state.json"
_LOCK = threading.Lock()

_DEFAULT: dict[str, Any] = {
    "privacy": {
        "camera": True,
        "screen": True,
        "browser": True,
        "files": True,
        "messaging": True,
        "remote": True,
    },
    "focus": {"active": False, "until": "", "started_at": ""},
    "voice": {"style": "balanced", "reply_length": "brief"},
    "local_llm": {"enabled": False, "model": "qwen2.5:7b"},
    "power_mode": {"enabled": False, "activated_at": ""},
    "tasks": [],
    "routines": [],
    "activity": [],
    "custom_tools": [],
}


def _load_unlocked() -> dict[str, Any]:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    state = deepcopy(_DEFAULT)
    for key, value in data.items() if isinstance(data, dict) else []:
        if key in state and isinstance(value, type(state[key])):
            state[key] = value
    return state


def _save_unlocked(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def get_state() -> dict[str, Any]:
    with _LOCK:
        return _load_unlocked()


def update_state(mutator) -> dict[str, Any]:
    with _LOCK:
        state = _load_unlocked()
        mutator(state)
        _save_unlocked(state)
        return deepcopy(state)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def record_activity(action: str, detail: str, status: str = "done") -> None:
    def mutate(state: dict[str, Any]) -> None:
        state["activity"].insert(0, {"at": now_iso(), "action": action, "detail": detail[:300], "status": status})
        del state["activity"][100:]
    update_state(mutate)