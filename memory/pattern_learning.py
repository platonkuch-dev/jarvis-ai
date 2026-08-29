"""
memory/pattern_learning.py — behavioral pattern learning.

Logs every tool call JARVIS makes to a small rolling local log. Once enough
new activity has accumulated, asks Claude to extract durable behavioral
patterns (habits, preferences, routines — not one-off actions) from the log
and folds them into the regular memory store under "preferences", so future
conversations can draw on them the same way as any other remembered fact.

Best-effort throughout: any failure here must never affect the main
conversation loop, so every public function swallows and logs its own errors.
"""
import json
import re
import threading
from datetime import datetime

from core.path_utils import get_user_data_dir
from core.config import CLAUDE_MODEL, PATTERN_ANALYZE_EVERY as ANALYZE_EVERY, PATTERN_MAX_LOG_LINES as MAX_LOG_LINES
from core.runtime_config import get_claude_api_key as _get_claude_key
from memory.memory_manager import update_memory

LOG_PATH      = get_user_data_dir() / "usage_log.jsonl"

# Tools whose calls are noise for behavioral analysis (memory bookkeeping itself,
# and pure housekeeping) — skip logging these so the log stays signal-dense.
_SKIP_TOOLS = {"save_memory", "forget_memory"}


def log_tool_call(name: str, args: dict) -> None:
    """Appends one usage record; once enough new activity has piled up, kicks
    off a background pattern analysis. Never raises."""
    if name in _SKIP_TOOLS:
        return
    try:
        entry = {
            "t":    datetime.now().strftime("%Y-%m-%d %H:%M"),
            "tool": name,
            # Keep args minimal and short — just a signal of *what kind* of
            # request it was, never full payloads (no screenshots, no file bytes).
            "args": {
                k: (v if isinstance(v, (int, float, bool)) else str(v)[:80])
                for k, v in (args or {}).items()
                if k not in ("code", "image", "content", "data")
            },
        }

        lines = []
        if LOG_PATH.exists():
            lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
        lines.append(json.dumps(entry, ensure_ascii=False))
        lines = lines[-MAX_LOG_LINES:]

        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

        if len(lines) % ANALYZE_EVERY == 0:
            threading.Thread(target=_analyze_patterns, args=(lines,), daemon=True).start()
    except Exception as e:
        print(f"[PatternLearning] ⚠️ log_tool_call failed: {e}")


def _analyze_patterns(lines: list[str]) -> None:
    claude_key = _get_claude_key()
    if not claude_key:
        return

    try:
        entries = [json.loads(l) for l in lines[-ANALYZE_EVERY * 3:] if l.strip()]
        if len(entries) < 10:
            return  # not enough signal yet

        prompt = f"""You observe how a user interacts with their voice assistant JARVIS over time.
Below is a log of recent tool calls (tool name, rough arguments, time).

Your job: extract 2-5 DURABLE behavioral patterns worth remembering — habits, preferences, routines.
Examples of good patterns: "usually asks for Python code, rarely other languages", "mostly active in
the evening", "prefers short answers over long explanations", "frequently checks the weather in the
morning". Do NOT restate individual one-off actions. Only include patterns with real repeated evidence
in the log below — if there isn't a clear pattern yet, return an empty JSON object {{}}.

Log:
{json.dumps(entries, ensure_ascii=False, indent=2)}

Return ONLY a JSON object: {{"short_snake_case_key": "one-sentence pattern description", ...}}
No markdown, no explanation."""

        import anthropic
        client = anthropic.Anthropic(api_key=claude_key)
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        text = re.sub(r"^```(?:json)?\s*", "", text.strip())
        text = re.sub(r"\s*```$", "", text)
        patterns = json.loads(text)

        if not isinstance(patterns, dict) or not patterns:
            return

        clean = {
            k: v for k, v in patterns.items()
            if isinstance(k, str) and isinstance(v, str) and v.strip()
        }
        if not clean:
            return

        update_memory({"preferences": {k: {"value": v} for k, v in clean.items()}})
        print(f"[PatternLearning] 🧠 Learned {len(clean)} behavioral pattern(s)")
    except Exception as e:
        print(f"[PatternLearning] ⚠️ analysis failed: {e}")
