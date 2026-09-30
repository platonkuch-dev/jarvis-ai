"""Behavioral pattern learning: periodically asks Claude to spot durable
habits/preferences in recent tool-call history and folds them into memory.

Ported from the original Jarvis project's memory/pattern_learning.py, with
one simplification: that version kept its own separate usage_log.jsonl,
capped in size; this one reads the tail of tools/_logging.py's own
TOOL_LOG_FILE instead (already the record of every tool call, and other
consumers -- the web UI's action log -- need its full history, so nothing
here truncates it).

Best-effort throughout: called from _logging.py's log_call wrapper as a
fire-and-forget background task, so any failure here must never affect the
tool call that triggered it.
"""

from __future__ import annotations

import json
import logging
import re

import anthropic

import config
import usage
from tools import claude_cli
from tools.memory import update_memory

logger = logging.getLogger("jarvis-voice-agent.pattern_learning")

# Tool calls that are noise for behavioral analysis (memory bookkeeping
# itself) -- skip counting these so the analysis cadence reflects real use.
_SKIP_TOOLS = {"remember_fact", "forget_fact", "list_memory"}

_call_count = 0


async def note_tool_call(tool_name: str) -> None:
    """Call after every successful tool invocation; analyzes every
    PATTERN_ANALYZE_EVERY calls once enough log history exists."""
    global _call_count
    if tool_name in _SKIP_TOOLS:
        return
    _call_count += 1
    if _call_count % config.PATTERN_ANALYZE_EVERY != 0:
        return
    try:
        await _analyze_patterns()
    except Exception as exc:
        logger.warning("pattern analysis failed: %s", exc)


async def _analyze_patterns() -> None:
    if not config.TOOL_LOG_FILE.exists():
        return

    lines = config.TOOL_LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    lines = lines[-config.PATTERN_MAX_LOG_LINES :]
    entries = []
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("tool") in _SKIP_TOOLS:
            continue
        entries.append(
            {
                "t": entry.get("ts", ""),
                "tool": entry.get("tool", ""),
                "args": {k: str(v)[:80] for k, v in (entry.get("args") or {}).items()},
            }
        )
    if len(entries) < 10:
        return  # not enough signal yet

    prompt = f"""You observe how a user interacts with their voice assistant Jarvis over time.
Below is a log of recent tool calls (tool name, rough arguments, time).

Your job: extract 2-5 DURABLE behavioral patterns worth remembering -- habits, preferences,
routines. Examples of good patterns: "usually asks for weather in the morning", "mostly active
in the evening", "prefers short answers over long explanations", "frequently opens VS Code and
Slack together". Do NOT restate individual one-off actions. Only include patterns with real
repeated evidence in the log below -- if there isn't a clear pattern yet, return an empty JSON
object {{}}.

Log:
{json.dumps(entries[-config.PATTERN_ANALYZE_EVERY * 3 :], ensure_ascii=False, indent=2)}

Return ONLY a JSON object: {{"short_snake_case_key": "one-sentence pattern description in Russian", ...}}
No markdown, no explanation."""

    # Tries the local `claude` CLI first -- tool-free, infrequent, and
    # latency-insensitive, so it bills against a Claude.ai subscription
    # instead of this project's metered API key when one is available (see
    # tools/claude_cli.py). Falls back to the direct API call otherwise.
    text = await claude_cli.ask(prompt)
    if text is None and config.SUBSCRIPTION_MODE:
        return
    if text is None:
        client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
        msg = await client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        usage.record_response(config.ANTHROPIC_MODEL, msg.usage, source="pattern_learning")
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)
    try:
        patterns = json.loads(text)
    except json.JSONDecodeError:
        return

    if not isinstance(patterns, dict) or not patterns:
        return
    clean = {k: v for k, v in patterns.items() if isinstance(k, str) and isinstance(v, str) and v.strip()}
    if not clean:
        return

    await update_memory({"preferences": {k: {"value": v} for k, v in clean.items()}})
    logger.info("learned %d behavioral pattern(s): %s", len(clean), list(clean.keys()))
