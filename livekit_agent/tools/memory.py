"""Long-term memory: structured facts about the user, persisted across
sessions and injected into the system prompt.

Ported from the original Jarvis project's memory/memory_manager.py -- same
six-category schema, same char-budget consolidation strategy -- adapted from
threading+sync to asyncio and from a bespoke lock to tools/_store.py's
JsonStore. `format_memory_for_prompt` in particular is close to line-for-line
the same function, just with the category headers in Russian.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Literal

import anthropic
from livekit.agents import RunContext, function_tool

import config
import usage
from tools import claude_cli, runtime
from tools._logging import log_call
from tools._store import JsonStore
from tools.registry import register_impl, register_tool

_memory_store = JsonStore(config.MEMORY_FILE, default={})

Category = Literal["identity", "preferences", "projects", "relationships", "wishes", "notes"]
_CATEGORIES: tuple[Category, ...] = (
    "identity",
    "preferences",
    "projects",
    "relationships",
    "wishes",
    "notes",
)


def _empty_memory() -> dict[str, dict]:
    return {cat: {} for cat in _CATEGORIES}


def _normalize(data: Any) -> dict[str, dict]:
    if not isinstance(data, dict):
        return _empty_memory()
    base = _empty_memory()
    for cat in _CATEGORIES:
        value = data.get(cat)
        base[cat] = value if isinstance(value, dict) else {}
    return base


async def load_memory() -> dict[str, dict]:
    return _normalize(await _memory_store.load())


def _all_entries(memory: dict) -> list[tuple[str, str, dict]]:
    entries = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            if isinstance(entry, dict) and "value" in entry:
                entries.append((cat, key, entry))
    return entries


def _truncate_value(val: str) -> str:
    if len(val) > config.MEMORY_MAX_VALUE_LEN:
        return val[: config.MEMORY_MAX_VALUE_LEN].rstrip() + "…"
    return val


async def _consolidate_with_claude(memory: dict) -> dict | None:
    """Asks Claude to merge duplicate/contradictory entries and tighten
    wording, same six-category schema in and out. Returns None (caller falls
    back to oldest-first trimming) if Claude isn't reachable (neither the
    local CLI nor the API key) or the response fails validation."""
    plain = {
        cat: {k: e["value"] for k, e in items.items() if isinstance(e, dict) and "value" in e}
        for cat, items in memory.items()
        if isinstance(items, dict)
    }

    prompt = f"""You maintain a compact personal-memory store for a voice assistant.
Below is the current memory as JSON (schema: category -> key -> value, all plain strings).
Space is limited, so:

1. Merge or remove genuinely redundant/duplicate entries.
2. If two entries contradict, keep only the more specific / more recent-sounding one.
3. Tighten wording -- shorter, but never drop a distinct fact.
4. If two facts are clearly related, you may combine them into one entry that reads
   naturally together, instead of two disconnected ones.
5. NEVER invent new facts. NEVER drop a fact that isn't truly redundant or contradicted.
6. Keep exactly these six top-level keys: identity, preferences, projects, relationships,
   wishes, notes. Keep each key inside a category as a short snake_case identifier.

Current memory:
{json.dumps(plain, ensure_ascii=False, indent=2)}

Return ONLY the consolidated memory as valid JSON, same schema -- no markdown, no explanation."""

    try:
        # Tries the local `claude` CLI first -- tool-free, infrequent, and
        # latency-insensitive, so it's a good fit for billing against a
        # Claude.ai subscription instead of this project's metered API key
        # (see tools/claude_cli.py). Falls back to the direct API call below
        # if the CLI is missing or fails for any reason.
        text = await claude_cli.ask(prompt)
        if text is None and config.SUBSCRIPTION_MODE:
            return None
        if text is None:
            client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
            msg = await client.messages.create(
                model=config.ANTHROPIC_MODEL,
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            usage.record_response(config.ANTHROPIC_MODEL, msg.usage, source="memory")
            text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        text = re.sub(r"^```(?:json)?\s*", "", text.strip())
        text = re.sub(r"\s*```$", "", text)
        result = json.loads(text)
    except Exception:
        return None

    if not isinstance(result, dict) or not set(result.keys()) <= set(_CATEGORIES):
        return None

    today = time.strftime("%Y-%m-%d")
    rebuilt = _empty_memory()
    for cat, items in result.items():
        if not isinstance(items, dict):
            continue
        for key, value in items.items():
            if not isinstance(key, str) or not isinstance(value, str) or not value.strip():
                continue
            old_entry = memory.get(cat, {}).get(key)
            old_value = old_entry.get("value") if isinstance(old_entry, dict) else None
            updated = (
                old_entry["updated"]
                if (isinstance(old_entry, dict) and old_value == value and "updated" in old_entry)
                else today
            )
            rebuilt[cat][key] = {"value": _truncate_value(value), "updated": updated}
    return rebuilt


async def _trim_to_limit(memory: dict) -> dict:
    if len(json.dumps(memory, ensure_ascii=False)) <= config.MEMORY_MAX_CHARS:
        return memory

    consolidated = await _consolidate_with_claude(memory)
    if consolidated is not None:
        memory = consolidated
        if len(json.dumps(memory, ensure_ascii=False)) <= config.MEMORY_MAX_CHARS:
            return memory

    entries = _all_entries(memory)
    entries.sort(key=lambda t: t[2].get("updated", "0000-00-00"))
    for cat, key, _ in entries:
        if len(json.dumps(memory, ensure_ascii=False)) <= config.MEMORY_MAX_CHARS:
            break
        del memory[cat][key]
    return memory


def _recursive_update(target: dict, updates: dict) -> bool:
    changed = False
    for key, value in updates.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, dict) and "value" not in value:
            if key not in target or not isinstance(target[key], dict):
                target[key] = {}
                changed = True
            if _recursive_update(target[key], value):
                changed = True
        else:
            new_val = _truncate_value(str(value["value"] if isinstance(value, dict) else value))
            existing = target.get(key, {})
            if not isinstance(existing, dict) or existing.get("value") != new_val:
                target[key] = {"value": new_val, "updated": time.strftime("%Y-%m-%d")}
                changed = True
    return changed


async def update_memory(memory_update: dict) -> dict:
    if not memory_update:
        return await load_memory()
    memory = await load_memory()
    if _recursive_update(memory, memory_update):
        memory = await _trim_to_limit(memory)
        await _memory_store.mutate(lambda _old: (memory, None))
        await _refresh_agent_instructions()
    return memory


def format_memory_for_prompt(memory: dict | None) -> str:
    """Ported near-verbatim from memory_manager.py, headers in Russian."""
    if not memory:
        return ""

    lines: list[str] = []

    identity = memory.get("identity", {})
    id_fields = ["name", "age", "birthday", "city", "job", "language", "school", "nationality"]
    for field in id_fields:
        entry = identity.get(field)
        if entry:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"{field}: {val}")
    for key, entry in identity.items():
        if key in id_fields:
            continue
        val = entry.get("value") if isinstance(entry, dict) else entry
        if val:
            lines.append(f"{key.replace('_', ' ')}: {val}")

    sections = [
        ("preferences", "Предпочтения:", 15),
        ("projects", "Проекты и цели:", 8),
        ("relationships", "Люди в жизни:", 10),
        ("wishes", "Желания и планы:", 8),
        ("notes", "Другие заметки:", 8),
    ]
    for cat, header, limit in sections:
        items = memory.get(cat, {})
        if not items:
            continue
        lines.append("")
        lines.append(header)
        for key, entry in list(items.items())[:limit]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key.replace('_', ' ')}: {val}")

    if not lines:
        return ""

    header = "[ЧТО ТЫ ЗНАЕШЬ ОБ ЭТОМ ЧЕЛОВЕКЕ — используй естественно, никогда не зачитывай как список]\n"
    result = header + "\n".join(lines)
    # The store itself is capped at MEMORY_MAX_CHARS; the rendered block (no
    # JSON quoting/dates) is shorter, so this only guards against runaway values.
    if len(result) > config.MEMORY_MAX_CHARS:
        result = result[: config.MEMORY_MAX_CHARS - 1] + "…"
    return result + "\n"


async def _refresh_agent_instructions() -> None:
    """Live sessions pick up new/removed facts immediately, not just next restart."""
    agent = runtime.get_active_agent()
    if agent is None:
        return
    import prompts

    memory = await load_memory()
    try:
        await agent.update_instructions(prompts.build_instructions(memory))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# remember_fact / forget_fact / list_memory
# ---------------------------------------------------------------------------


@register_impl("remember_fact")
@log_call("remember_fact")
async def _remember_fact(*, key: str, value: str, category: str = "notes") -> dict:
    if category not in _CATEGORIES:
        category = "notes"
    key = key.strip().lower().replace(" ", "_")
    value = value.strip()
    if not key or not value:
        return {"status": "error", "message": "Нужны и ключ, и значение, чтобы что-то запомнить."}
    await update_memory({category: {key: {"value": value}}})
    return {"status": "ok", "message": f"Запомнил: {value}"}


@register_tool
@function_tool
async def remember_fact(context: RunContext, key: str, value: str, category: Category = "notes") -> str:
    """Save a durable fact or preference about the user for future sessions.

    Call this whenever the user shares something worth remembering long-term
    -- especially when they explicitly say "запомни". Use a short
    snake_case-ish key that identifies the fact (e.g. "name", "city",
    "favorite_language") and keep value short and self-contained.

    Args:
        key: Short identifier for this fact, e.g. "name" or "favorite_editor".
        value: The fact itself, e.g. "Иван" or "предпочитает VS Code".
        category: One of "identity" (name/age/city/job/...), "preferences",
            "projects" (goals, active projects), "relationships" (people in
            their life), "wishes" (plans/wants), or "notes" (anything else).
    """
    result = await _remember_fact(key=key, value=value, category=category)
    return result["message"]


@register_impl("forget_fact")
@log_call("forget_fact")
async def _forget_fact(*, key: str, category: str = "notes") -> dict:
    memory = await load_memory()
    items = memory.get(category, {})
    key = key.strip().lower().replace(" ", "_")
    if key not in items:
        return {"status": "not_found", "message": f"Не нашёл «{key}» в категории «{category}»."}
    removed = items.pop(key)
    await _memory_store.mutate(lambda _old: (memory, None))
    await _refresh_agent_instructions()
    return {"status": "ok", "message": f"Забыл: {removed.get('value', key)}"}


@register_tool
@function_tool
async def forget_fact(context: RunContext, key: str, category: Category = "notes") -> str:
    """Remove a previously remembered fact that's outdated or wrong.

    Args:
        key: The identifier used when the fact was remembered (see remember_fact).
        category: The category it was stored under.
    """
    result = await _forget_fact(key=key, category=category)
    return result["message"]


@register_impl("list_memory")
@log_call("list_memory")
async def _list_memory() -> dict:
    memory = await load_memory()
    formatted = format_memory_for_prompt(memory)
    if not formatted:
        return {"status": "ok", "message": "Пока ничего не запомнил."}
    return {"status": "ok", "message": formatted}


@register_tool
@function_tool
async def list_memory(context: RunContext) -> str:
    """List everything currently remembered about the user."""
    result = await _list_memory()
    return result["message"]
