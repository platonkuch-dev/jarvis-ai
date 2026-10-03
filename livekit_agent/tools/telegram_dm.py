"""Sends Telegram messages from Jarvis's own dedicated Telegram account
(config.TELEGRAM_*) via Telethon (MTProto), not a Bot API bot -- a real
account can message anyone on Telegram, where a bot can't message someone
who hasn't messaged it first.

Requires a one-time interactive login (see telegram_login.py) before any of
this works -- until then _get_client() raises, which the caller turns into
an ordinary error result rather than a crash.
"""

from __future__ import annotations

import re
import sys

from livekit.agents import RunContext, function_tool
from telethon import TelegramClient
from telethon.tl.functions.contacts import ImportContactsRequest
from telethon.tl.types import InputPhoneContact

import config
import telegram_contacts
import tg_session
from tools._logging import log_call
from tools.memory import load_memory
from tools.registry import register_impl, register_tool

_client: TelegramClient | None = None


async def _get_client() -> TelegramClient:
    global _client
    if _client is None:
        # Both voice workers (desktop console + phone) load this module; on
        # the shared main session file they locked each other out.
        role = "_worker_console" if "console" in sys.argv else "_worker_phone"
        _client = TelegramClient(
            tg_session.own_copy(config.TELEGRAM_SESSION_PATH + role),
            config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH,
        )
    if not _client.is_connected():
        await tg_session.connect_with_retry(_client)
    if not await _client.is_user_authorized():
        raise RuntimeError(
            "Telegram-аккаунт Джарвиса ещё не авторизован -- нужно один раз "
            "запустить `python telegram_login.py` вручную и ввести код из Telegram."
        )
    return _client


_PHONE_RE = re.compile(r"\+?\d[\d\s\-]{7,}\d")


_TOKEN_SPLIT = re.compile(r"[\s_,.;:()\"'«»-]+")


async def _resolve_phone(recipient: str) -> str | None:
    """Looks a spoken name up in memory: any entry (any category, so it works
    however the assistant filed "remember Egor's number") whose key or text
    contains that name as a whole word AND holds a phone number. Anything
    else is left for the caller to try as a number or @username as typed.

    Raises LookupError when the name matches two different numbers --
    guessing would message the wrong person."""
    wanted = {w for w in _TOKEN_SPLIT.split(recipient.strip().lower()) if w}
    if not wanted:
        return None
    memory = await load_memory()
    numbers: set[str] = set()
    for items in memory.values():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            value = entry.get("value") if isinstance(entry, dict) else entry
            match = _PHONE_RE.search(value or "")
            if match and wanted <= set(_TOKEN_SPLIT.split(f"{key} {value}".lower())):
                numbers.add(re.sub(r"[\s\-]", "", match.group()))
    if len(numbers) > 1:
        raise LookupError(f"В памяти несколько разных номеров для «{recipient}» — уточните, кому именно.")
    return next(iter(numbers), None)


@register_impl("send_telegram_message")
@log_call("send_telegram_message")
async def _send_telegram_message(*, recipient: str, message: str) -> dict:
    if not config.TELEGRAM_API_ID or not config.TELEGRAM_API_HASH:
        return {"status": "error", "message": "Telegram-аккаунт Джарвиса не настроен (нет API ID/Hash)."}

    try:
        phone = await _resolve_phone(recipient)
    except LookupError as exc:
        return {"status": "error", "message": str(exc)}
    target = phone or recipient.strip()

    try:
        client = await _get_client()
        if target.lstrip("+").isdigit():
            # A phone number can only be resolved to a chat once it's a
            # contact of this account -- import (idempotent; re-importing an
            # existing contact is a harmless no-op) then use the returned user.
            imported = await client(
                ImportContactsRequest(
                    [InputPhoneContact(client_id=0, phone=target, first_name=recipient, last_name="")]
                )
            )
            if not imported.users:
                return {
                    "status": "error",
                    "message": f"Номер {target} не найден в Telegram -- сообщение не отправить.",
                }
            entity = imported.users[0]
        else:
            entity = await client.get_entity(target)
        await client.send_message(entity, message)
    except RuntimeError as exc:
        return {"status": "error", "message": str(exc)}
    except Exception as exc:
        return {"status": "error", "message": f"Не удалось отправить сообщение в Telegram: {exc}"}

    return {"status": "ok", "message": f"Сообщение отправлено {recipient} в Telegram."}


@register_tool
@function_tool
async def send_telegram_message(context: RunContext, recipient: str, message: str) -> str:
    """Sends a text message in Telegram from Jarvis's own dedicated Telegram
    account (a real account, not a bot -- can message anyone on Telegram,
    with no "must message first" restriction a bot would have).

    Args:
        recipient: Who to message -- a saved contact's name from memory
            (e.g. "Егор", "Катя", "Яна"), a phone number in international
            format (+380...), or a Telegram @username.
        message: The text to send.
    """
    result = await _send_telegram_message(recipient=recipient, message=message)
    return result["message"]


@register_impl("remember_person_fact")
@log_call("remember_person_fact")
async def _remember_person_fact(*, person_name: str, fact: str) -> dict:
    message = await telegram_contacts.remember_person_fact(person_name, fact)
    return {"status": "ok", "message": message}


@register_tool
@function_tool
async def remember_person_fact(context: RunContext, person_name: str, fact: str) -> str:
    """Remembers a fact ABOUT a specific, DIFFERENT, NAMED person (never about
    the user themselves, and never in a chat where that person is the one
    currently talking -- use remember_contact_note for that instead), so
    Jarvis can bring it up when THAT person is the one chatting with it in
    Telegram -- e.g. the user says "Катя моя девушка" (about someone else,
    Катя, while talking to Jarvis themselves) and later, when Катя messages
    Jarvis, it already knows this about her. Matches the name against people
    who have messaged Jarvis on Telegram before (Cyrillic/Latin spelling
    doesn't matter, e.g. "Катя" matches a Telegram account named "katya"); if
    nobody matching has messaged yet, the fact is saved and attached
    automatically the first time a matching name does.

    Only use this for a fact ABOUT someone else that's fine for them to see
    reflected back at them in their own conversation -- everything saved
    here can surface in that person's chat, unlike tools/memory.py's facts
    (which are about the user only and never shown to anyone else).

    Args:
        person_name: The person's name, as the user said it (e.g. "Катя").
        fact: The fact to remember about them, in the same language the user said it.
    """
    result = await _remember_person_fact(person_name=person_name, fact=fact)
    return result["message"]
