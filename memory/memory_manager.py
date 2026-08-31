import json
import re
from datetime import datetime
from threading import Lock
from pathlib import Path

from core.path_utils import get_memory_path
from core.config import CLAUDE_MODEL, MEMORY_MAX_CHARS, MEMORY_MAX_VALUE_LEN as MAX_VALUE_LENGTH
from core.runtime_config import get_claude_api_key as _get_claude_key, build_anthropic_client as _build_anthropic_client


MEMORY_PATH      = get_memory_path()
_lock            = Lock()

def _empty_memory() -> dict:
    return {
        "identity":      {},
        "preferences":   {},
        "projects":      {},
        "relationships": {},
        "wishes":        {},
        "notes":         {},
    }

def load_memory() -> dict:
    if not MEMORY_PATH.exists():
        return _empty_memory()
    with _lock:
        try:
            data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                base = _empty_memory()
                for key in base:
                    if key not in data:
                        data[key] = {}
                return data
            return _empty_memory()
        except Exception as e:
            print(f"[Memory] ⚠️ Load error: {e}")
            return _empty_memory()

def _all_entries(memory: dict) -> list[tuple]:
    entries = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            if isinstance(entry, dict) and "value" in entry:
                entries.append((cat, key, entry))
    return entries


def _consolidate_with_claude(memory: dict) -> dict | None:
    """Asks Claude to merge duplicate/redundant entries, resolve contradictions, and
    tighten wording so related facts read as connected notes instead of raw key-value
    pairs — same six-category schema in, same schema out. Returns None (no-op) if
    Claude isn't configured or the response fails validation, so the caller can fall
    back to the old blind oldest-first trim safely."""
    claude_key = _get_claude_key()
    if not claude_key:
        return None

    plain: dict = {}
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        plain[cat] = {
            key: entry["value"]
            for key, entry in items.items()
            if isinstance(entry, dict) and "value" in entry
        }

    prompt = f"""You maintain a compact personal-memory store for a voice assistant.
Below is the current memory as JSON (schema: category -> key -> value, all plain strings).
Space is limited, so:

1. Merge or remove genuinely redundant/duplicate entries.
2. If two entries contradict, keep only the more specific / more recent-sounding one.
3. Tighten wording — shorter, but never drop a distinct fact.
4. If two facts are clearly related (e.g. a project and a preference tied to it), you may
   combine them into one entry that reads naturally together, instead of two disconnected ones.
5. NEVER invent new facts. NEVER drop a fact that isn't truly redundant or contradicted.
6. Keep exactly these six top-level keys: identity, preferences, projects, relationships, wishes, notes.
   Keep each key inside a category as a short snake_case identifier.

Current memory:
{json.dumps(plain, ensure_ascii=False, indent=2)}

Return ONLY the consolidated memory as valid JSON, same schema — no markdown, no explanation."""

    try:
        client = _build_anthropic_client()
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        text = re.sub(r"^```(?:json)?\s*", "", text.strip())
        text = re.sub(r"\s*```$", "", text)
        result = json.loads(text)
    except Exception as e:
        print(f"[Memory] ⚠️ Claude consolidation failed: {e}")
        return None

    valid_cats = set(_empty_memory().keys())
    if not isinstance(result, dict) or not set(result.keys()) <= valid_cats:
        print("[Memory] ⚠️ Claude consolidation returned an unexpected schema — ignoring")
        return None

    today   = datetime.now().strftime("%Y-%m-%d")
    rebuilt = _empty_memory()
    for cat, items in result.items():
        if not isinstance(items, dict):
            continue
        for key, value in items.items():
            if not isinstance(key, str) or not isinstance(value, str) or not value.strip():
                continue
            old_entry = memory.get(cat, {}).get(key)
            old_value = old_entry.get("value") if isinstance(old_entry, dict) else None
            updated   = old_entry["updated"] if (isinstance(old_entry, dict) and old_value == value and "updated" in old_entry) else today
            rebuilt[cat][key] = {"value": _truncate_value(value), "updated": updated}

    return rebuilt


def _trim_to_limit(memory: dict) -> dict:
    if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
        return memory

    consolidated = _consolidate_with_claude(memory)
    if consolidated is not None:
        print("[Memory] 🧠 Consolidated via Claude")
        memory = consolidated
        if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
            return memory

    entries = _all_entries(memory)
    entries.sort(key=lambda t: t[2].get("updated", "0000-00-00"))
    for cat, key, _ in entries:
        if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
            break
        del memory[cat][key]
        print(f"[Memory] 🗑️  Trimmed {cat}/{key}")
    return memory

def save_memory(memory: dict) -> None:
    if not isinstance(memory, dict):
        return
    memory = _trim_to_limit(memory)
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        MEMORY_PATH.write_text(
            json.dumps(memory, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def _truncate_value(val: str) -> str:
    if isinstance(val, str) and len(val) > MAX_VALUE_LENGTH:
        return val[:MAX_VALUE_LENGTH].rstrip() + "…"
    return val


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
            new_val  = _truncate_value(str(value["value"] if isinstance(value, dict) else value))
            entry    = {"value": new_val, "updated": datetime.now().strftime("%Y-%m-%d")}
            existing = target.get(key, {})
            if not isinstance(existing, dict) or existing.get("value") != new_val:
                target[key] = entry
                changed = True
    return changed


def update_memory(memory_update: dict) -> dict:
    if not isinstance(memory_update, dict) or not memory_update:
        return load_memory()
    memory = load_memory()
    if _recursive_update(memory, memory_update):
        save_memory(memory)
        print(f"[Memory] 💾 Saved: {list(memory_update.keys())}")
    return memory

def format_memory_for_prompt(memory: dict | None) -> str:
    if not memory:
        return ""

    lines = []

    identity  = memory.get("identity", {})
    id_fields = ["name", "age", "birthday", "city", "job", "language", "school", "nationality"]
    for field in id_fields:
        entry = identity.get(field)
        if entry:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"{field.title()}: {val}")
    for key, entry in identity.items():
        if key in id_fields:
            continue
        val = entry.get("value") if isinstance(entry, dict) else entry
        if val:
            lines.append(f"{key.replace('_', ' ').title()}: {val}")

    prefs = memory.get("preferences", {})
    if prefs:
        lines.append("")
        lines.append("Preferences:")
        for key, entry in list(prefs.items())[:15]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key.replace('_', ' ').title()}: {val}")

    projects = memory.get("projects", {})
    if projects:
        lines.append("")
        lines.append("Active Projects / Goals:")
        for key, entry in list(projects.items())[:8]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key.replace('_', ' ').title()}: {val}")

    rels = memory.get("relationships", {})
    if rels:
        lines.append("")
        lines.append("People in their life:")
        for key, entry in list(rels.items())[:10]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key.replace('_', ' ').title()}: {val}")

    wishes = memory.get("wishes", {})
    if wishes:
        lines.append("")
        lines.append("Wishes / Plans / Wants:")
        for key, entry in list(wishes.items())[:8]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key.replace('_', ' ').title()}: {val}")

    notes = memory.get("notes", {})
    if notes:
        lines.append("")
        lines.append("Other notes:")
        for key, entry in list(notes.items())[:8]:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"  - {key}: {val}")

    if not lines:
        return ""

    header = "[WHAT YOU KNOW ABOUT THIS PERSON — use naturally, never recite like a list]\n"
    result = header + "\n".join(lines)
    if len(result) > 2000:
        result = result[:1997] + "…"

    return result + "\n"

def remember(key: str, value: str, category: str = "notes") -> str:
    valid = {"identity", "preferences", "projects", "relationships", "wishes", "notes"}
    if category not in valid:
        category = "notes"
    update_memory({category: {key: {"value": value}}})
    return f"Remembered: {category}/{key} = {value}"


def forget(key: str, category: str = "notes") -> str:
    memory = load_memory()
    cat    = memory.get(category, {})
    if key in cat:
        del cat[key]
        memory[category] = cat
        save_memory(memory)
        return f"Forgotten: {category}/{key}"
    return f"Not found: {category}/{key}"


forget_memory = forget