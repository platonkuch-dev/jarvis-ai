"""Shared contact-notes store for Telegram: who Jarvis has talked to and
what it has learned about them, keyed by Telegram sender id and separate
from tools/memory.py's facts (which are about the account OWNER only, never
about a third party).

Written from two different OS processes: telegram_bridge.py (which updates
a contact's record every time they message, and handles remember_contact_note
calls during that conversation) and tools/telegram_dm.py's
remember_person_fact (called from the voice worker, or from the owner's own
Telegram conversation, running inside telegram_bridge.py's own process --
either way, a different call site than the one above). Every write goes
through JsonStore's atomic tmp-file-replace, so concurrent writes from two
processes can't corrupt the file; no cross-process lock is needed for this
personal-scale, low-frequency use.

A fact the owner shares about someone who hasn't messaged the bridge yet has
nowhere to attach to -- TELEGRAM_PENDING_NOTES_FILE holds those, keyed by
the name as given, merged into the real contact record the moment a
matching name first messages (see touch_contact).
"""

from __future__ import annotations

import time
from typing import Any

import config
from tools._store import JsonStore

_contacts_store = JsonStore(config.TELEGRAM_CONTACTS_FILE, default={})
_pending_store = JsonStore(config.TELEGRAM_PENDING_NOTES_FILE, default={})

# Real bug hit live: the owner told Jarvis about "Катя" (Cyrillic), but her
# actual Telegram first name is "katya" (Latin) -- a plain case-insensitive
# compare never matches those, so the pending note silently never attached.
# Transliterating both sides to a common form before comparing catches this;
# not a full linguistic transliteration, just enough for name-matching.
_translit = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
})


def _normalize_name(name: str) -> str:
    """Lowercases and transliterates Cyrillic to a Latin form comparable to a
    Latin-script name, then strips everything but letters/digits, so
    "Катя"/"katya"/"Katya!"/"катя" all reduce to the same string."""
    lowered = name.strip().lower()
    translit = lowered.translate(_translit)
    return "".join(c for c in translit if c.isalnum())


def _new_record(name: str) -> dict[str, Any]:
    return {"name": name, "first_seen": time.strftime("%Y-%m-%d %H:%M:%S"),
            "last_seen": time.strftime("%Y-%m-%d %H:%M:%S"), "message_count": 0, "notes": []}


async def add_notes(sender_id: int, notes: list[str]) -> dict[str, Any]:
    def _mutate(data: dict) -> tuple[dict, dict]:
        key = str(sender_id)
        rec = data.get(key) or _new_record("")
        existing = rec.get("notes") or []
        existing.extend(notes)
        rec["notes"] = existing[-config.TELEGRAM_CONTACT_MAX_NOTES:]
        data[key] = rec
        return data, rec
    return await _contacts_store.mutate(_mutate)


async def add_note(sender_id: int, note: str) -> dict[str, Any]:
    return await add_notes(sender_id, [note])


async def _pop_pending_notes(name: str) -> list[str]:
    if not name:
        return []
    key = _normalize_name(name)
    if not key:
        return []

    def _mutate(data: dict) -> tuple[dict, list[str]]:
        notes = data.pop(key, [])
        return data, notes
    return await _pending_store.mutate(_mutate)


async def _add_pending_note(name: str, note: str) -> None:
    key = _normalize_name(name)

    def _mutate(data: dict) -> tuple[dict, None]:
        notes = data.get(key) or []
        notes.append(note)
        data[key] = notes[-config.TELEGRAM_CONTACT_MAX_NOTES:]
        return data, None
    await _pending_store.mutate(_mutate)


async def touch_contact(sender_id: int, sender_name: str) -> dict[str, Any]:
    """Creates/updates this sender's record (name, first/last seen, message
    count), merges in any pending notes left for a matching name before they
    ever messaged, and returns the current record."""
    def _mutate(data: dict) -> tuple[dict, dict]:
        key = str(sender_id)
        rec = data.get(key) or _new_record(sender_name)
        if sender_name:
            rec["name"] = sender_name
        rec["last_seen"] = time.strftime("%Y-%m-%d %H:%M:%S")
        rec["message_count"] = rec.get("message_count", 0) + 1
        data[key] = rec
        return data, rec
    rec = await _contacts_store.mutate(_mutate)

    pending = await _pop_pending_notes(sender_name)
    if pending:
        rec = await add_notes(sender_id, pending)
    return rec


def contact_context(rec: dict[str, Any]) -> str:
    bits = [f"имя: {rec.get('name') or 'неизвестно'}", f"первое сообщение: {rec.get('first_seen', '?')}",
            f"сообщений всего: {rec.get('message_count', 0)}"]
    notes = rec.get("notes") or []
    if notes:
        bits.append("заметки: " + "; ".join(notes))
    return "\n\n[Информация об этом собеседнике из прошлых разговоров -- " + "; ".join(bits) + ".]"


async def find_by_name(name: str) -> list[tuple[int, dict[str, Any]]]:
    """Transliteration- and case-insensitive substring match on stored
    contact names (a name given in Cyrillic matches a Telegram first name
    stored in Latin script, and vice versa)."""
    data = await _contacts_store.load()
    q = _normalize_name(name)
    if not q:
        return []
    return [(int(k), v) for k, v in data.items() if q in _normalize_name(v.get("name") or "")]


async def remember_person_fact(person_name: str, fact: str) -> str:
    """Attaches `fact` to the contact whose name matches `person_name` (used
    to surface it back when that person is the one chatting), or queues it
    as pending if nobody with that name has messaged the bridge yet."""
    matches = await find_by_name(person_name)
    if len(matches) == 1:
        sender_id, rec = matches[0]
        await add_note(sender_id, fact)
        return f"Запомнил про {rec.get('name') or person_name}: {fact}"
    if len(matches) > 1:
        names = ", ".join(f"{rec.get('name')} (id {sid})" for sid, rec in matches)
        return f"Нашёл несколько подходящих контактов: {names}. Уточните, кого именно вы имеете в виду."
    await _add_pending_note(person_name, fact)
    return f"{person_name} ещё не писал(а) мне в Telegram — запомню и расскажу это ей/ему, как только она/он напишет."
