"""
Lightweight latency profiler for tool execution.

JARVIS's voice pipeline (STT/reasoning/TTS) runs entirely inside Gemini
Live's single audio session — there's no local wake-word/STT/TTS stage to
instrument separately, that's all server-side and opaque to this codebase.
What IS measurable and actually under our control is everything AFTER
Gemini decides to call a tool: dispatch + the tool's own execution time.
That's what this module tracks, per call, plus a rolling per-tool-name
summary so "what's actually slow" is visible from the logs instead of
guessed at.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

_lock = threading.Lock()
_recent: dict[str, deque] = defaultdict(lambda: deque(maxlen=50))
_call_count = 0
_SUMMARY_EVERY = 15


def record(tool_name: str, elapsed_ms: float) -> None:
    global _call_count
    with _lock:
        _recent[tool_name].append(elapsed_ms)
        _call_count += 1
        due = _call_count % _SUMMARY_EVERY == 0
    print(f"[Latency] tool={tool_name} elapsed={elapsed_ms:.0f}ms")
    if due:
        print_summary()


def summary() -> dict[str, dict[str, float]]:
    """Per-tool avg/min/max/count over the last (up to 50) recorded calls —
    call this periodically or on demand to see where time is actually going."""
    with _lock:
        out = {}
        for name, samples in _recent.items():
            if not samples:
                continue
            out[name] = {
                "count": len(samples),
                "avg_ms": sum(samples) / len(samples),
                "min_ms": min(samples),
                "max_ms": max(samples),
            }
        return out


def print_summary(top_n: int = 10) -> None:
    stats = summary()
    if not stats:
        print("[Latency] No calls recorded yet.")
        return
    ranked = sorted(stats.items(), key=lambda kv: kv[1]["avg_ms"], reverse=True)
    print(f"[Latency] Slowest tools (avg over last calls, top {top_n}):")
    for name, s in ranked[:top_n]:
        print(f"  {name:<20} avg={s['avg_ms']:.0f}ms  min={s['min_ms']:.0f}ms  max={s['max_ms']:.0f}ms  n={s['count']}")


class timed:
    """Context manager: `with timed('open_app'): ...` records elapsed ms."""

    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        self._start = 0.0

    def __enter__(self) -> "timed":
        self._start = time.monotonic()
        return self

    def __exit__(self, *_exc) -> None:
        record(self.tool_name, (time.monotonic() - self._start) * 1000)
