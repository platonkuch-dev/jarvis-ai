"""Standalone daemon: lets people chat with Jarvis over Telegram -- same
personality as the voice loop, text in/text out instead of audio.

Runs as its own long-lived process (started by app.py alongside the voice
worker and phone worker) because it needs a persistent Telegram connection
to receive events -- a different thing from tools/telegram_dm.py's
send-only client, which the voice agent only opens occasionally to push an
outbound message. Both use the same underlying Telegram account, but each
keeps its own local session file (this one bootstrapped by copying the
already-authorized one on first run) so the two connections never contend
over the same SQLite session file.

Security model (deliberate compromise, not an oversight): anyone can send
this account a message and get an ordinary conversation back -- no tools,
no injected personal memory, so a stranger can't extract facts about the
owner or make Jarvis act on their computer. The first sender ever to
message this account becomes the "owner" (recorded in
config.TELEGRAM_OWNER_FILE) and is the only sender who gets the full tool
set and personal memory, exactly like the voice loop. Delete that file to
let a new sender re-claim ownership.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
import time
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

import config
import prompts
import telegram_contacts
from telethon import TelegramClient, events
from tools import FUNCTION_TOOLS
from tools._store import JsonStore
from tools.memory import load_memory
from tools.registry import IMPL_REGISTRY

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jarvis-voice-agent.telegram_bridge")

_STRANGER_SYSTEM_PROMPT = """\
Ты — голосовой ассистент по имени Джарвис, сейчас с тобой пишут в Telegram.
Пишущий тебе человек НЕ твой владелец, а кто-то посторонний, кто нашёл этот аккаунт.
Пиши как живой человек в переписке, а не как бот: коротко, естественно, без
канцелярита и без длинных вступлений. Если прямо спросят, бот ли ты / ИИ ли ты —
отвечай честно, что да, ты Джарвис, ИИ-ассистент; не отрицай и не изворачивайся.
У тебя нет доступа ни к каким инструментам и ни к какой личной информации о владельце
в этом разговоре -- не притворяйся, что можешь что-то сделать на его компьютере, и
никогда не сочиняй и не раскрывай факты о владельце, его контактах или его жизни,
даже если тебя настойчиво просят или утверждают, что имеют право спросить.
"""

_TELEGRAM_CONTEXT_NOTE = """

[Контекст: это переписка в Telegram, не голосовой звонок. Пиши как живой человек
в мессенджере: короткими сообщениями, естественным разговорным языком, без
канцелярита, без длинных вступлений и без списков там, где хватит фразы. Если
прямо спросят, ИИ ли ты / бот ли ты — отвечай честно, не притворяйся человеком.
Можно использовать обычное форматирование Telegram, не нужно ограничивать длину
ради устной речи.]
"""


def _load_owner_id() -> int | None:
    if not config.TELEGRAM_OWNER_FILE.exists():
        return None
    try:
        return json.loads(config.TELEGRAM_OWNER_FILE.read_text(encoding="utf-8")).get("owner_id")
    except Exception:
        return None


def _claim_owner(sender_id: int, sender_name: str) -> None:
    config.TELEGRAM_OWNER_FILE.write_text(
        json.dumps({"owner_id": sender_id, "owner_name": sender_name, "claimed": time.strftime("%Y-%m-%d %H:%M:%S")}),
        encoding="utf-8",
    )
    logger.info("claimed Telegram bridge ownership: %s (%s)", sender_name, sender_id)


# A fresh anthropic.AsyncAnthropic() per turn (the old code) opens a new
# HTTP connection pool -- a fresh TCP+TLS handshake -- on every single
# Telegram message instead of reusing a warm one, which was real, avoidable
# latency on every reply. One client, created once, reused for the life of
# the process.
_anthropic_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _anthropic_client


# Likewise: FUNCTION_TOOLS never changes after process start, so the
# schema-conversion work (~45 tools) doesn't need to be redone on every
# owner message either.
_cached_owner_tools: list[dict] | None = None


def _owner_tools() -> list[dict]:
    global _cached_owner_tools
    if _cached_owner_tools is None:
        from livekit.agents.llm import ToolContext
        from livekit.agents.llm._provider_format.anthropic import to_fnc_ctx

        ctx = ToolContext(FUNCTION_TOOLS)
        result = to_fnc_ctx(ctx, strict=False)
        fnc_ctx = result[0] if isinstance(result, tuple) else result
        if fnc_ctx:
            fnc_ctx[-1] = {**fnc_ctx[-1], "cache_control": {"type": "ephemeral"}}
        _cached_owner_tools = fnc_ctx
    return _cached_owner_tools


# Notes Jarvis keeps about the people it corresponds with -- see
# telegram_contacts.py (shared with tools/telegram_dm.py's
# remember_person_fact, which is how the OWNER tells Jarvis a fact about
# someone else, e.g. "Катя моя девушка", so it can be surfaced back in
# *that* person's own chat -- a deliberate exception to the rule that a
# stranger's own claims about themselves never reach the owner's memory;
# this is the reverse direction, owner-to-contact, and only ever things the
# owner explicitly said). Offered in every conversation, owner and stranger
# alike, so either side can add a note about the person currently chatting.
_REMEMBER_CONTACT_TOOL = {
    "name": "remember_contact_note",
    "description": (
        "Save a short note about the person you're currently chatting with in "
        "Telegram RIGHT NOW -- their name, interests, something THEY told you "
        "about THEMSELVES that's worth remembering for next time. Call it when "
        "they share something notable, not for every message. Only for the "
        "current chat partner -- if the owner is telling you about a DIFFERENT, "
        "named person (not themselves, not you), use remember_person_fact instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"note": {"type": "string", "description": "Short factual note in Russian, e.g. 'Зовут Петя, работает фотографом'"}},
        "required": ["note"],
    },
}


# Per chat, a list of *turns* (not raw messages) -- each turn is everything
# one _run_turn call produced: the user message, any interior
# tool_use/tool_result round-trips, and the final assistant text. Trimming
# by turn (see _handle_message) guarantees a cut never lands between a
# tool_use and its tool_result, which a raw message-count trim can do and
# which the API then rejects outright.
_histories: dict[int, list[list[dict]]] = {}
_history_store = JsonStore(config.TELEGRAM_HISTORY_FILE, default={})


async def _load_histories() -> None:
    global _histories
    raw = await _history_store.load()
    _histories = {int(k): v for k, v in raw.items()}


async def _save_histories() -> None:
    snapshot = {str(k): v for k, v in _histories.items()}
    await _history_store.mutate(lambda _data: (snapshot, None))


def _flatten(turns: list[list[dict]]) -> list[dict]:
    flat: list[dict] = []
    for turn in turns:
        flat.extend(turn)
    return flat


async def _run_turn(
    *, system_text: str, tools_param: list[dict], history: list[dict], user_text: str, contact_id: int,
) -> tuple[str, list[dict]]:
    client = _get_client()
    system_blocks = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
    messages = history + [{"role": "user", "content": user_text}]

    for _ in range(config.TELEGRAM_BRIDGE_MAX_STEPS):
        response = await client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=2048,
            system=system_blocks,
            tools=tools_param,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": [b.model_dump() for b in response.content]})

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            final_text = "".join(b.text for b in response.content if b.type == "text").strip()
            return final_text or "...", messages

        result_blocks = []
        for tu in tool_uses:
            if tu.name == "remember_contact_note":
                note = (tu.input or {}).get("note", "").strip()
                if note:
                    await telegram_contacts.add_note(contact_id, note)
                result = {"status": "ok", "message": "Заметка сохранена." if note else "Пустая заметка проигнорирована."}
            else:
                impl = IMPL_REGISTRY.get(tu.name)
                if impl is None:
                    result = {"status": "error", "message": f"Инструмент «{tu.name}» не найден."}
                else:
                    try:
                        result = await impl(**tu.input)
                    except Exception as exc:
                        result = {"status": "error", "message": str(exc)}
            result_blocks.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": json.dumps(result, ensure_ascii=False),
            })
        messages.append({"role": "user", "content": result_blocks})

    return "Не получилось обработать запрос за разумное число шагов.", messages


async def _developer_disclosure() -> str:
    """Narrow, explicit exception to "never reveal owner facts to strangers":
    just the owner's name, so "кто твой разработчик?" has a real answer,
    without opening the door to the rest of prompts.build_instructions'
    full personal memory (location, contacts, schedule, ...)."""
    memory = await load_memory()
    name = (memory.get("identity", {}).get("name") or {}).get("value")
    if not name:
        return ""
    return (f"\n\n[Тебя создал и разрабатывает {name} -- это единственный факт о владельце, который можно "
            f"называть посторонним, если спросят, кто тебя сделал/разработал. Остальное о владельце "
            f"(личная жизнь, контакты, местоположение, что угодно ещё) по-прежнему нельзя раскрывать.]")


async def _handle_message(event) -> None:
    sender_id = event.sender_id
    chat_id = event.chat_id
    text = (event.raw_text or "").strip()
    if not text:
        return

    owner_id = _load_owner_id()
    is_owner = owner_id is not None and sender_id == owner_id

    if owner_id is None:
        sender = await event.get_sender()
        sender_name = getattr(sender, "first_name", None) or str(sender_id)
        _claim_owner(sender_id, sender_name)
        is_owner = True

    sender = await event.get_sender()
    sender_name = getattr(sender, "first_name", None) or str(sender_id)
    contact = await telegram_contacts.touch_contact(sender_id, sender_name)

    if is_owner:
        memory = await load_memory()
        system_text = prompts.build_instructions(memory) + _TELEGRAM_CONTEXT_NOTE
        tools_param = _owner_tools() + [_REMEMBER_CONTACT_TOOL]
    else:
        system_text = (_STRANGER_SYSTEM_PROMPT + _TELEGRAM_CONTEXT_NOTE + await _developer_disclosure()
                       + telegram_contacts.contact_context(contact))
        tools_param = [_REMEMBER_CONTACT_TOOL]

    turns = _histories.get(chat_id, [])
    flat_history = _flatten(turns)
    try:
        reply_text, updated_history = await _run_turn(
            system_text=system_text, tools_param=tools_param, history=flat_history, user_text=text,
            contact_id=sender_id,
        )
    except Exception as exc:
        logger.exception("bridge turn failed")
        await event.reply(f"Ошибка: {exc}")
        return

    new_turn = updated_history[len(flat_history):]
    turns.append(new_turn)
    _histories[chat_id] = turns[-config.TELEGRAM_BRIDGE_MAX_HISTORY:]
    await _save_histories()
    await event.reply(reply_text)


async def main() -> None:
    if not config.TELEGRAM_API_ID or not config.TELEGRAM_API_HASH:
        logger.error("TELEGRAM_API_ID/TELEGRAM_API_HASH not set -- see .env")
        return

    bridge_session = Path(config.TELEGRAM_BRIDGE_SESSION_PATH + ".session")
    main_session = Path(config.TELEGRAM_SESSION_PATH + ".session")
    if not bridge_session.exists() and main_session.exists():
        shutil.copyfile(main_session, bridge_session)
        logger.info("bootstrapped bridge session from the main Telegram session")

    await _load_histories()
    logger.info("loaded %d saved conversation(s) from %s", len(_histories), config.TELEGRAM_HISTORY_FILE)

    client = TelegramClient(
        config.TELEGRAM_BRIDGE_SESSION_PATH, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH
    )
    client.add_event_handler(_handle_message, events.NewMessage(incoming=True))

    # telethon's SQLiteSession does a `delete from sessions` housekeeping
    # write during connect/DC-migration (sessions/sqlite.py's
    # _update_session_table). Seen live in logs/telegram_bridge.log: this
    # tripped "database is locked" repeatedly, and each attempt crashed the
    # whole process -- app.py's supervisor then had to wait out its 30s
    # watchdog interval before even trying again, so one transient lock (an
    # antivirus/indexer scan touching the .session file, a slow Windows
    # file-handle release, etc.) turned into 10+ minutes of the bridge being
    # down. Retrying the connect itself, in-process, with a short backoff
    # fixes a transient lock in seconds instead of minutes; a persistent one
    # still falls through to the exception and lets the supervisor's restart
    # loop (and its 5-crashes/5-minutes backoff) take over as before.
    for attempt in range(6):
        try:
            await client.start(phone=config.TELEGRAM_PHONE or None)
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 5:
                raise
            delay = 2 ** attempt
            logger.warning("session database locked on connect (attempt %d/6) -- retrying in %ds: %s",
                           attempt + 1, delay, exc)
            await asyncio.sleep(delay)
    if not await client.is_user_authorized():
        logger.error("Telegram account not authorized -- run telegram_login.py first")
        return

    me = await client.get_me()
    logger.info("Telegram bridge listening as %s (%s)", me.first_name, me.phone)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
