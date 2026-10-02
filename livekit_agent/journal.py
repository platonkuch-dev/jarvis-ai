"""One shared journal of what Jarvis hears and says, across every channel.

Voice conversations (worker.py), the Telegram chats of Jarvis's own account
and of the owner's personal account (chat_memory.py) all append here, one
JSON line per message, one file per day under data/journal/. chat_memory.py
condenses it into a short digest (and owner facts for tools/memory.py), and
prompts.build_instructions puts that digest into every owner-facing prompt --
voice and Telegram alike -- so Jarvis remembers in one place what was said
where, instead of each channel knowing only its own conversation.

Entry: {"ts": epoch, "ch": channel, "chat": chat title, "who": speaker,
        "role": "owner" | "jarvis" | "other", "text": ...}
channel: "voice", "tg_jarvis" (Jarvis's Telegram account), "tg_personal"
(the owner's own account).

Messages from other people are data, never instructions: whoever reads the
journal back into a prompt (chat_memory.digest_block) labels them as such.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import config

JOURNAL_DIR = config.DATA_DIR / "journal"
MAX_TEXT = 2000
KEEP_DAYS = 60
CHANNEL_NAMES = {"voice": "голос", "tg_jarvis": "Telegram Джарвиса", "tg_personal": "личный Telegram"}

_lock = threading.Lock()


def _day_file(ts: float) -> Path:
    return JOURNAL_DIR / (time.strftime("%Y-%m-%d", time.localtime(ts)) + ".jsonl")


def append(channel: str, chat: str, who: str, role: str, text: str, ts: float | None = None) -> None:
    """Never raises: losing a journal line must not break a conversation."""
    text = (text or "").strip()
    if not text:
        return
    ts = ts or time.time()
    entry = {"ts": round(ts, 3), "ch": channel, "chat": chat or "", "who": who or "", "role": role,
             "text": text[:MAX_TEXT]}
    try:
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with _lock, _day_file(ts).open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def read_since(since_ts: float, limit: int = 5000) -> list[dict[str, Any]]:
    """Entries newer than since_ts, oldest first (at most `limit`, the newest ones)."""
    if not JOURNAL_DIR.exists():
        return []
    first_day = time.strftime("%Y-%m-%d", time.localtime(max(since_ts, 0)))
    entries: list[dict[str, Any]] = []
    for path in sorted(JOURNAL_DIR.glob("*.jsonl")):
        if path.stem < first_day:
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("ts", 0) > since_ts:
                    entries.append(e)
        except OSError:
            continue
    entries.sort(key=lambda e: e.get("ts", 0))
    return entries[-limit:]


def recent(hours: float = 24, limit: int = 200) -> list[dict[str, Any]]:
    return read_since(time.time() - hours * 3600, limit)


def format_entry(e: dict[str, Any], *, with_channel: bool = True) -> str:
    when = time.strftime("%d.%m %H:%M", time.localtime(e.get("ts", 0)))
    where = CHANNEL_NAMES.get(e.get("ch", ""), e.get("ch", ""))
    chat = e.get("chat") or ""
    place = f"{where}, {chat}" if with_channel and chat else (where if with_channel else chat)
    return f"[{when}{', ' + place if place else ''}] {e.get('who') or '?'}: {e.get('text', '')}"


def prune(keep_days: int = KEEP_DAYS) -> None:
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - keep_days * 86400))
    for path in JOURNAL_DIR.glob("*.jsonl"):
        if path.stem < cutoff:
            path.unlink(missing_ok=True)
