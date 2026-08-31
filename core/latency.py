"""
Lightweight latency profiler for tool execution AND the Gemini Live
round trip around it.

Most of what this module tracks is dispatch + a tool's own execution time
under names like "web_search" or "window_manager" -- everything AFTER
Gemini decides to call a tool, fully within this codebase's control.

Gemini Live's own STT+reasoning+TTS-start time has no discrete
request/response boundary to hook (it's one continuous full-duplex audio
session, not a call-and-wait API), so it can't be broken down into STT vs.
reasoning vs. TTS -- but the aggregate CAN be timed from outside, by
comparing timestamps of events this codebase already receives: the last
input_transcription chunk before a turn, and the first sign of a reply
(audio byte / output_transcription / tool_call). See
core/audio_pipeline.py's _mark_response_started(), which records that gap
under "smart_path_first_signal" (plain conversational turn, or the turn
where Gemini decides to call a tool) and "smart_path_tool_reaction" (from
send_tool_response() to Gemini's next reply signal) -- both land in the
same per-name rolling summary below as any tool name.
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
