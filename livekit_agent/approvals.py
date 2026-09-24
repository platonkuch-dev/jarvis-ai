"""Owner approvals over Telegram for risky steps taken while nobody is at the PC.

An autonomous task (tasks.py) that reaches a step policy.py marks as
"confirm" can't ask out loud -- the owner may be away. Instead it calls
`request_approval()`, which queues a question to the owner's Telegram via
notify.py and waits. The owner answers "да <код>" / "нет <код>" (or just
"да"/"нет" when only one question is open); telegram_bridge.py hands that
reply to `handle_owner_reply()`, which records the decision in a small JSON
file the waiting process polls. File-based on purpose: the asker (voice
worker) and the listener (Telegram bridge) are different processes.

No answer before the timeout counts as "no".
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid

import config
import notify
from tools._store import read_json, write_json

APPROVALS_FILE = config.DATA_DIR / "approvals.json"
_POLL_S = 2.0

_YES = ("да", "yes", "ок", "ok", "подтверждаю", "можно", "давай", "+")
_NO = ("нет", "no", "не надо", "отмена", "стоп", "-")


def _load() -> dict:
    data = read_json(APPROVALS_FILE, {})
    return data if isinstance(data, dict) else {}


def _prune(data: dict) -> dict:
    """Drop decided/expired entries older than a day so the file stays small."""
    cutoff = time.time() - 86400
    return {k: v for k, v in data.items() if v.get("created", 0) > cutoff}


def create(question: str, timeout_s: float) -> str:
    approval_id = uuid.uuid4().hex[:4]
    data = _prune(_load())
    data[approval_id] = {"question": question, "status": "pending",
                         "created": time.time(), "expires": time.time() + timeout_s}
    write_json(APPROVALS_FILE, data)
    return approval_id


def status(approval_id: str) -> str:
    entry = _load().get(approval_id)
    if entry is None:
        return "denied"
    if entry["status"] == "pending" and time.time() > entry.get("expires", 0):
        return "expired"
    return entry["status"]


def _set(approval_id: str, new_status: str) -> None:
    data = _load()
    if approval_id in data:
        data[approval_id]["status"] = new_status
        write_json(APPROVALS_FILE, data)


def pending() -> dict[str, dict]:
    now = time.time()
    return {k: v for k, v in _load().items() if v["status"] == "pending" and v.get("expires", 0) > now}


async def request_approval(question: str, timeout_s: float | None = None) -> bool:
    """Ask the owner on Telegram and wait for the answer. False on "no" or timeout."""
    timeout_s = timeout_s or config.APPROVAL_TIMEOUT_S
    approval_id = create(question, timeout_s)
    minutes = max(1, round(timeout_s / 60))
    notify.notify_owner(
        f"Нужно ваше решение: {question}\n\nОтветьте «да {approval_id}» или «нет {approval_id}» "
        f"(жду {minutes} мин, без ответа — не делаю).",
        kind="approval",
    )
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        st = status(approval_id)
        if st == "approved":
            return True
        if st in ("denied", "expired"):
            return False
        await asyncio.sleep(_POLL_S)
    _set(approval_id, "expired")
    return False


def _parse(text: str) -> tuple[str | None, str | None]:
    """-> (decision "approved"/"denied" or None, explicit id or None)."""
    t = text.strip().lower()
    m = re.fullmatch(r"/?(approve|deny|да|нет|yes|no)\s+([0-9a-f]{4})", t)
    if m:
        word, code = m.groups()
        return ("approved" if word in ("approve", "да", "yes") else "denied"), code
    t = t.rstrip("!. ")
    if t in _YES:
        return "approved", None
    if t in _NO:
        return "denied", None
    return None, None


async def handle_owner_reply(text: str) -> str | None:
    """Called for every owner message. Returns a reply if the message was an
    answer to an open question, None if it's an ordinary message."""
    decision, code = _parse(text)
    if decision is None:
        return None
    open_items = pending()
    if code is None:
        if len(open_items) != 1:
            return None  # plain "да" with 0 or 2+ open questions: an ordinary chat message
        code = next(iter(open_items))
    if code not in open_items:
        return f"Вопрос {code} уже закрыт или истёк."
    _set(code, decision)
    word = "Разрешено" if decision == "approved" else "Отменено"
    return f"{word}: {open_items[code]['question']}"
