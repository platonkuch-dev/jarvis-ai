"""Tiny JSON-file-backed store shared by scheduling.py and scenarios.py.

Single process, so an `asyncio.Lock` per file path is enough to serialize
read-modify-write cycles -- no external DB needed for a local voice agent.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

_locks: dict[Path, asyncio.Lock] = {}


def _lock_for(path: Path) -> asyncio.Lock:
    if path not in _locks:
        _locks[path] = asyncio.Lock()
    return _locks[path]


def read_json(path: Path, default: Any) -> Any:
    # A fresh copy every time: handing out the shared default list/dict let a
    # caller's in-place mutation leak into every later read of any missing file.
    if not path.exists():
        return copy.deepcopy(default)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return copy.deepcopy(default)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


class JsonStore:
    """Async-safe read-modify-write helper bound to one JSON file."""

    def __init__(self, path: Path, default: Any) -> None:
        self.path = path
        self.default = default

    async def load(self) -> Any:
        async with _lock_for(self.path):
            return read_json(self.path, self.default)

    async def mutate(self, fn: Any) -> Any:
        """`fn(data) -> (new_data, return_value)`; persists new_data, returns return_value."""
        async with _lock_for(self.path):
            data = read_json(self.path, self.default)
            new_data, ret = fn(data)
            write_json(self.path, new_data)
            return ret
