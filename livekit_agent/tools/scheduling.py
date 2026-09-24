"""Local calendar/todo/reminder/timer tools.

Everything here is a flat JSON file next to the agent -- there is no real
calendar integration. Reminders and timers are persisted and
fired by `reminder_loop()` (see below), so they survive restarts.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timedelta

from livekit.agents import RunContext, function_tool

import config
from tools import runtime
from tools._logging import log_call
from tools._store import JsonStore
from tools.registry import register_impl, register_tool

_events_store = JsonStore(config.EVENTS_FILE, default=[])
_reminders_store = JsonStore(config.REMINDERS_FILE, default=[])
_todos_store = JsonStore(config.TODOS_FILE, default=[])


def _parse_datetime(value: str) -> datetime | None:
    try:
        from dateutil import parser as dateutil_parser

        return dateutil_parser.parse(value, dayfirst=True, fuzzy=True)
    except Exception:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------------------
# Calendar events
# ---------------------------------------------------------------------------


@register_impl("create_event")
@log_call("create_event")
async def _create_event(*, title: str, datetime_str: str, duration_minutes: int = 30) -> dict:
    when = _parse_datetime(datetime_str)
    if when is None:
        return {"status": "error", "message": f"Не смог разобрать дату/время «{datetime_str}»."}

    event = {
        "id": uuid.uuid4().hex[:8],
        "title": title,
        "start": when.isoformat(),
        "duration_minutes": duration_minutes,
    }

    def _mutate(data: list) -> tuple[list, dict]:
        data.append(event)
        return data, event

    await _events_store.mutate(_mutate)
    return {
        "status": "ok",
        "message": f"Событие «{title}» добавлено на {when.strftime('%d.%m %H:%M')}.",
        "event": event,
    }


@register_tool
@function_tool
async def create_event(
    context: RunContext, title: str, datetime_str: str, duration_minutes: int = 30
) -> str:
    """Create a calendar event in the local schedule.

    Args:
        title: Short title of the event.
        datetime_str: When it starts, in any natural date/time form, e.g. "2026-09-20 15:00".
        duration_minutes: Duration in minutes, defaults to 30.
    """
    result = await _create_event(title=title, datetime_str=datetime_str, duration_minutes=duration_minutes)
    return result["message"]


@register_impl("get_schedule")
@log_call("get_schedule")
async def _get_schedule(*, date_str: str) -> dict:
    day = _parse_datetime(date_str)
    if day is None:
        return {"status": "error", "message": f"Не смог разобрать дату «{date_str}»."}

    events = await _events_store.load()
    same_day = [e for e in events if datetime.fromisoformat(e["start"]).date() == day.date()]
    same_day.sort(key=lambda e: e["start"])

    if not same_day:
        return {"status": "ok", "message": f"На {day.strftime('%d.%m')} событий нет.", "events": []}

    listing = "; ".join(
        f"{datetime.fromisoformat(e['start']).strftime('%H:%M')} — {e['title']}" for e in same_day
    )
    return {"status": "ok", "message": f"На {day.strftime('%d.%m')}: {listing}.", "events": same_day}


@register_tool
@function_tool
async def get_schedule(context: RunContext, date_str: str) -> str:
    """List events scheduled for a given day.

    Args:
        date_str: The day to look up, e.g. "сегодня", "2026-09-20", "20.09".
    """
    result = await _get_schedule(date_str=date_str)
    return result["message"]


# ---------------------------------------------------------------------------
# Reminders & timers
# ---------------------------------------------------------------------------


# Reminders and timers are only records in REMINDERS_FILE; nothing waits on
# them in memory. `reminder_loop()` (started once by the voice worker in
# console mode) polls the file and fires whatever is due. So a reminder
# survives a crash/restart, and one created from another process (the
# Telegram bridge, a background task) still fires in the voice worker.

_POLL_S = 5.0
# Fired this late (PC was off/asleep, worker was down) -> say so explicitly.
_LATE_S = 120.0


async def _add_reminder(text: str, when: datetime, kind: str) -> dict:
    reminder = {"id": uuid.uuid4().hex[:8], "text": text, "at": when.isoformat(), "kind": kind}

    def _mutate(data: list) -> tuple[list, dict]:
        data.append(reminder)
        return data, reminder

    return await _reminders_store.mutate(_mutate)


async def take_due_reminders(now: datetime | None = None) -> list[dict]:
    """Removes and returns every reminder whose time has come."""
    now = now or datetime.now()

    def _mutate(data: list) -> tuple[list, list]:
        due, keep = [], []
        for r in data:
            try:
                (due if datetime.fromisoformat(r["at"]) <= now else keep).append(r)
            except (KeyError, ValueError):
                continue  # malformed entry: drop it rather than crash the loop forever
        return keep, due

    return await _reminders_store.mutate(_mutate)


def _announcement(reminder: dict, now: datetime) -> str:
    at = datetime.fromisoformat(reminder["at"])
    late = (now - at).total_seconds() > _LATE_S
    if reminder.get("kind") == "timer":
        text = f"Таймер {reminder.get('text') or ''} истёк.".replace("  ", " ")
    else:
        text = f"Напоминание: {reminder['text']}"
    if late:
        text = f"Пропущенное (было на {at.strftime('%d.%m %H:%M')}). {text}"
    return text


async def reminder_loop() -> None:
    """Fires due reminders out loud; copies them to Telegram when nobody is
    likely listening (no live session, or the agent is asleep)."""
    import notify

    while True:
        try:
            now = datetime.now()
            for reminder in await take_due_reminders(now):
                text = _announcement(reminder, now)
                if not runtime.user_present():
                    notify.notify_owner(f"⏰ {text}", kind="reminder")
                await runtime.say(text)
        except Exception:
            import logging

            logging.getLogger("jarvis-voice-agent.scheduling").exception("reminder loop tick failed")
        await asyncio.sleep(_POLL_S)


@register_impl("create_reminder")
@log_call("create_reminder")
async def _create_reminder(*, text: str, datetime_str: str) -> dict:
    when = _parse_datetime(datetime_str)
    if when is None:
        return {"status": "error", "message": f"Не смог разобрать дату/время «{datetime_str}»."}
    if when < datetime.now():
        return {"status": "error", "message": "Указанное время уже в прошлом."}

    reminder = await _add_reminder(text, when, "reminder")
    return {
        "status": "ok",
        "message": f"Напомню «{text}» в {when.strftime('%d.%m %H:%M')}.",
        "reminder": reminder,
    }


@register_tool
@function_tool
async def create_reminder(context: RunContext, text: str, datetime_str: str) -> str:
    """Set a one-time reminder for later. Survives restarts; if the user is away
    it is also sent to their Telegram.

    Args:
        text: What to remind the user about.
        datetime_str: When to remind them, in any natural date/time form.
    """
    result = await _create_reminder(text=text, datetime_str=datetime_str)
    return result["message"]


@register_impl("set_timer")
@log_call("set_timer")
async def _set_timer(*, minutes: float, label: str = "") -> dict:
    if minutes <= 0:
        return {"status": "error", "message": "Длительность таймера должна быть больше нуля."}
    await _add_reminder(label or f"на {minutes:g} минут", datetime.now() + timedelta(minutes=minutes), "timer")
    return {"status": "ok", "message": f"Таймер на {minutes:g} минут запущен."}


@register_tool
@function_tool
async def set_timer(context: RunContext, minutes: float) -> str:
    """Start a countdown timer that announces itself out loud when it ends.

    Args:
        minutes: Timer duration in minutes.
    """
    result = await _set_timer(minutes=minutes)
    return result["message"]


# ---------------------------------------------------------------------------
# Todos
# ---------------------------------------------------------------------------


@register_impl("add_todo")
@log_call("add_todo")
async def _add_todo(*, text: str) -> dict:
    todo = {
        "id": uuid.uuid4().hex[:8],
        "text": text,
        "done": False,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    def _mutate(data: list) -> tuple[list, dict]:
        data.append(todo)
        return data, todo

    await _todos_store.mutate(_mutate)
    return {"status": "ok", "message": f"Добавил в список задач: «{text}».", "todo": todo}


@register_tool
@function_tool
async def add_todo(context: RunContext, text: str) -> str:
    """Add an item to the local to-do list.

    Args:
        text: The task description.
    """
    result = await _add_todo(text=text)
    return result["message"]


@register_impl("complete_todo")
@log_call("complete_todo")
async def _complete_todo(*, query: str) -> dict:
    q = query.strip().lower()

    def _mutate(data: list) -> tuple[list, dict | None]:
        matches = [t for t in data if not t["done"] and q in t["text"].lower()]
        if not matches:
            return data, None
        matches[0]["done"] = True
        return data, matches[0]

    matched = await _todos_store.mutate(_mutate)
    if matched is None:
        return {"status": "not_found", "message": f"Не нашёл незавершённую задачу по «{query}»."}
    return {"status": "ok", "message": f"Задача «{matched['text']}» отмечена выполненной."}


@register_tool
@function_tool
async def complete_todo(context: RunContext, query: str) -> str:
    """Mark a to-do item as done by matching part of its text.

    Args:
        query: Text that identifies the task to complete.
    """
    result = await _complete_todo(query=query)
    return result["message"]
