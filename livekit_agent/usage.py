"""Local spend tracker + daily budget guard.

Every paid LLM call we can see reports its token usage here; the dollar
estimate is summed per day in config.USAGE_FILE. `budget_left()` is what
background tasks and use_computer check before starting -- once the day's
spend passes config.DAILY_BUDGET_USD they refuse, and the owner is told once
on Telegram. Plain conversation is never blocked: being unable to talk to
the assistant (even to say "stop") would be worse than the overspend.

Prices are Anthropic first-party $/1M tokens (input, output); cache writes
bill at 1.25x input, cache reads at 0.1x input. Unknown models fall back to
the Sonnet price -- deliberately on the high side, so the guard errs toward
stopping early rather than late.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import config
from tools._store import read_json, write_json

logger = logging.getLogger("jarvis-voice-agent.usage")

_PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-fable-5-1": (10.0, 50.0),
}
_FALLBACK_PRICE = (3.0, 15.0)
# Serializes read-modify-write of the usage file between threads of one
# process (use_computer runs its loop on a worker thread).
_lock = threading.Lock()


def price_for(model: str) -> tuple[float, float]:
    model = (model or "").lower()
    # Longest prefix first so "claude-opus-5-5" wins over "claude-opus-5".
    for prefix in sorted(_PRICES, key=len, reverse=True):
        if model.startswith(prefix):
            return _PRICES[prefix]
    return _FALLBACK_PRICE


def cost_usd(model: str, input_tokens: int = 0, output_tokens: int = 0,
             cache_read: int = 0, cache_write: int = 0) -> float:
    p_in, p_out = price_for(model)
    return (input_tokens * p_in + cache_write * p_in * 1.25 + cache_read * p_in * 0.1
            + output_tokens * p_out) / 1_000_000


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def record(model: str, input_tokens: int = 0, output_tokens: int = 0,
           cache_read: int = 0, cache_write: int = 0, *, source: str = "") -> float:
    """Adds one call's cost to today's total. Never raises."""
    usd = cost_usd(model, input_tokens, output_tokens, cache_read, cache_write)
    try:
        with _lock:
            data = read_json(config.USAGE_FILE, {})
            if not isinstance(data, dict):
                data = {}
            day = data.setdefault(_today(), {"usd": 0.0, "by_source": {}})
            day["usd"] = round(day["usd"] + usd, 6)
            key = source or model
            day["by_source"][key] = round(day["by_source"].get(key, 0.0) + usd, 6)
            # Keep ~a month of history.
            for old in sorted(data)[:-31]:
                data.pop(old, None)
            write_json(config.USAGE_FILE, data)
    except Exception:
        logger.warning("could not record usage", exc_info=True)
    return usd


def record_response(model: str, usage: Any, *, source: str = "") -> float:
    """Convenience for an Anthropic SDK `response.usage` object."""
    if usage is None:
        return 0.0
    return record(
        model,
        getattr(usage, "input_tokens", 0) or 0,
        getattr(usage, "output_tokens", 0) or 0,
        getattr(usage, "cache_read_input_tokens", 0) or 0,
        getattr(usage, "cache_creation_input_tokens", 0) or 0,
        source=source,
    )


def spent_today() -> float:
    data = read_json(config.USAGE_FILE, {})
    day = data.get(_today()) if isinstance(data, dict) else None
    return float(day.get("usd", 0.0)) if isinstance(day, dict) else 0.0


def budget_left() -> float | None:
    """Dollars left today, or None when no limit is configured."""
    if config.DAILY_BUDGET_USD <= 0:
        return None
    return config.DAILY_BUDGET_USD - spent_today()


def check_budget(what: str) -> str | None:
    """None if `what` may start; otherwise a user-facing refusal message
    (and a one-per-day Telegram notice to the owner)."""
    left = budget_left()
    if left is None or left > 0:
        return None
    marker = config.DATA_DIR / "budget_notified.txt"
    try:
        already = marker.read_text(encoding="utf-8").strip() == _today()
    except OSError:
        already = False
    if not already:
        import notify

        notify.notify_owner(
            f"💸 Дневной лимит ${config.DAILY_BUDGET_USD:g} на ИИ исчерпан "
            f"(потрачено ≈${spent_today():.2f}). Фоновые задачи и работа с экраном "
            f"остановлены до завтра. Лимит меняется в панели.",
            kind="budget",
        )
        try:
            marker.write_text(_today(), encoding="utf-8")
        except OSError:
            pass
    return (f"Не запускаю {what}: дневной лимит расходов ${config.DAILY_BUDGET_USD:g} исчерпан. "
            f"Его можно поднять в панели, раздел «Дополнительно».")
