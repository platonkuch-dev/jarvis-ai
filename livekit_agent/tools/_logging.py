"""Append-only JSONL audit log for every tool call, whichever path invoked it.

Because `run_scenario` calls the same `IMPL_REGISTRY` functions that the LLM's
function-calling path calls, decorating the implementation (not the
`@function_tool` wrapper) logs both paths for free.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from config import DATA_DIR, LOG_MAX_BYTES, TOOL_LOG_FILE

# The last tool call and whether it's still running -- the HUD lights up the neuron of that block.
ACTIVITY_FILE = DATA_DIR / "tool_activity.json"

_F = TypeVar("_F", bound=Callable[..., Awaitable[Any]])


def _write(entry: dict[str, Any]) -> None:
    line = json.dumps(entry, ensure_ascii=False, default=str)
    try:
        if TOOL_LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            os.replace(TOOL_LOG_FILE, TOOL_LOG_FILE.with_suffix(TOOL_LOG_FILE.suffix + ".1"))
    except OSError:
        pass
    with open(TOOL_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _note_activity(tool_name: str, started: float, done: bool) -> None:
    try:
        tmp = ACTIVITY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"tool": tool_name, "at": started, "done": done}), encoding="utf-8")
        os.replace(tmp, ACTIVITY_FILE)
    except OSError:
        pass


def _note_pattern_learning(tool_name: str) -> None:
    """Fire-and-forget hook into tools/pattern_learning.py. Deferred import:
    pattern_learning -> memory -> this module would otherwise be a real
    circular import at module-load time; by call time (long after every
    module has finished loading) it's harmless."""
    try:
        from tools.pattern_learning import note_tool_call

        asyncio.create_task(note_tool_call(tool_name))
    except Exception:
        pass


def log_call(tool_name: str) -> Callable[[_F], _F]:
    """Decorator for an IMPL_REGISTRY function: logs ts/tool/args/result/error/duration."""

    def deco(fn: _F) -> _F:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.time()
            entry: dict[str, Any] = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
                "tool": tool_name,
                "args": kwargs,
            }
            _note_activity(tool_name, started, False)
            try:
                result = await fn(*args, **kwargs)
                entry["ok"] = True
                entry["result"] = result
                _note_pattern_learning(tool_name)
                return result
            except Exception as exc:  # noqa: BLE001 - we want every failure logged, then re-raised
                entry["ok"] = False
                entry["error"] = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                entry["duration_ms"] = round((time.time() - started) * 1000, 1)
                _write(entry)
                _note_activity(tool_name, started, True)

        return wrapper  # type: ignore[return-value]

    return deco
