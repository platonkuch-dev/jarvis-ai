"""Feeds what Claude Code learns about the owner into Jarvis's long-term memory.

Claude Code keeps its own notes as Markdown files under
~/.claude/projects/<project>/memory/. A PostToolUse hook (Write|Edit) runs

    runtime\\python.exe app\\memory_sync.py --hook

with the hook's JSON on stdin, as an async hook (Claude Code doesn't wait
for it). A detached child would not survive: Claude Code runs hooks inside a
Windows job that takes the whole process tree down when the hook exits. If
the written file is one of those memory notes, it asks claude (the fast
main model, on the subscription -- haiku kept re-adding known facts under
new keys) to turn the note into
short Russian facts a personal assistant should know, and merges them with
tools.memory.update_memory (the same path remember_fact uses: size limit,
consolidation). Jarvis's brain picks them up on its next start -- every
wake from sleep restarts claude with a freshly built prompt.

Unchanged notes are skipped (content hash in data/memory_sync_state.json),
so re-saving a file costs nothing. Logs: logs/memory_sync.log.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sys
from pathlib import Path

import config
from atomic_io import atomic_write_text

logger = logging.getLogger("jarvis-voice-agent.memory_sync")

STATE_FILE = config.DATA_DIR / "memory_sync_state.json"
_MEMORY_DIR = re.compile(r"[\\/]\.claude[\\/]projects[\\/][^\\/]+[\\/]memory[\\/][^\\/]+\.md$", re.IGNORECASE)

PROMPT = """Ты обновляешь долговременную память голосового ассистента Джарвиса.
Его владелец — {owner}. Ниже заметка, которую Claude Code (ассистент-программист владельца)
только что сохранил о нём или об их общих проектах, и текущая память Джарвиса.

Извлеки из заметки факты, которые пригодятся Джарвису в повседневном общении с владельцем:
кто он, что любит и не любит, как ему удобнее, люди в его жизни, его проекты и цели,
его компьютер и дом, договорённости о том, как Джарвис должен себя вести.
Пропускай детали реализации кода (имена файлов и функций, флаги, внутренние баги),
если они не нужны владельцу в разговоре. Не выдумывай ничего, чего нет в заметке.
Строго без дублей: если в памяти уже есть факт на ту же тему (даже другими словами или под
другим ключом) — либо пропусти его, либо верни ТОТ ЖЕ существующий ключ с уточнённым значением.
Новый ключ — только для действительно новой темы. Пути, реестр, имена процессов и версии — только
если без них владельцу не помочь; лучше одной фразой по-человечески.

Пиши по-русски, коротко (до 300 символов на факт). О самом Джарвисе — от первого лица («я»), о владельце — по имени, если оно известно.
Ответ — ТОЛЬКО JSON без markdown: {{"категория": {{"ключ_snake_case": "значение"}}}},
категории только: identity, preferences, projects, relationships, wishes, notes.
Если полезного нет — верни {{}}.

Текущая память Джарвиса:
{memory}

Заметка Claude Code ({name}):
{note}
"""


def _setup_logging() -> None:
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=config.LOGS_DIR / "memory_sync.log", level=logging.INFO, encoding="utf-8",
        format="%(asctime)s %(levelname)s %(message)s",
    )


def is_memory_note(path: str) -> bool:
    return bool(_MEMORY_DIR.search(path)) and Path(path).name.upper() != "MEMORY.MD"


def _hook_path() -> str | None:
    """The memory note the hook event wrote, or None for any other file."""
    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace") or "{}")
    except ValueError:
        return None
    tool_input = event.get("tool_input") or {}
    path = tool_input.get("file_path") or (event.get("tool_response") or {}).get("filePath") or ""
    return path if path and is_memory_note(path) else None


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    atomic_write_text(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=1))


def _parse_facts(text: str) -> dict:
    from tools.memory import _CATEGORIES

    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    match = re.search(r"\{.*\}", text, re.DOTALL)
    data = json.loads(match.group(0)) if match else {}
    facts: dict = {}
    for cat, items in (data.items() if isinstance(data, dict) else []):
        if cat not in _CATEGORIES or not isinstance(items, dict):
            continue
        for key, value in items.items():
            if isinstance(key, str) and isinstance(value, str) and value.strip():
                key = re.sub(r"[^\w]+", "_", key.strip().lower()).strip("_")[:60]
                if key:
                    facts.setdefault(cat, {})[key] = value.strip()
    return facts


async def sync_note(path: Path) -> dict:
    """Extracts facts from one Claude memory note and merges them; returns what was added."""
    import cc_agent
    from tools.memory import load_memory, update_memory

    note = path.read_text(encoding="utf-8")
    digest = hashlib.sha256(note.encode("utf-8")).hexdigest()
    state = _load_state()
    if state.get(str(path)) == digest:
        logger.info("unchanged, skipped: %s", path.name)
        return {}

    memory = await load_memory()
    plain = {c: {k: e.get("value") for k, e in items.items() if isinstance(e, dict)} for c, items in memory.items()}
    try:
        owner = (plain.get("identity") or {}).get("name") or "владелец, его имя пока неизвестно"
        reply = await cc_agent.ask(PROMPT.format(
            owner=owner, memory=json.dumps(plain, ensure_ascii=False, indent=1), name=path.name, note=note[:12000],
        ), model=config.CLAUDE_CODE_MODEL, timeout=240)
    except Exception as exc:
        logger.warning("claude call failed: %s", exc)
        reply = None
    if reply is None:
        logger.warning("claude unavailable, %s not synced (will retry on its next save)", path.name)
        return {}
    facts = _parse_facts(reply)
    if facts:
        await update_memory(facts)
    state[str(path)] = digest
    _save_state(state)
    logger.info("%s -> %s", path.name, json.dumps(facts, ensure_ascii=False) if facts else "nothing new")
    return facts


def main() -> None:
    paths = [_hook_path()] if "--hook" in sys.argv else sys.argv[1:]
    if not any(paths):
        return
    _setup_logging()
    for arg in filter(None, paths):
        path = Path(arg)
        if not path.is_file():
            continue
        try:
            asyncio.run(sync_note(path))
        except Exception:
            logger.exception("sync failed for %s", path)


if __name__ == "__main__":
    main()
