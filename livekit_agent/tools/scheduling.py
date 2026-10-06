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


_RELATIVE_DAYS = {"позавчера": -2, "вчера": -1, "сегодня": 0, "завтра": 1, "послезавтра": 2}


def _parse_datetime(value: str) -> datetime | None:
    """Natural date/time -> datetime. Handles ISO ("2026-10-02 15:00"), Russian
    day-first dates ("02.10", "2.10.2026 в 9:30"), relative days ("завтра в 15:00",
    "сегодня") and "через 20 минут / 2 часа". A bare day means its 00:00."""
    import re

    text = (value or "").strip().lower().replace("ё", "е")
    if not text:
        return None
    now = datetime.now()

    m = re.fullmatch(r"через\s+(\d+(?:[.,]\d+)?)?\s*(минут\w*|мин|час\w*|ч|дн\w*|день)", text)
    if m:
        amount = float((m.group(1) or "1").replace(",", "."))
        unit = m.group(2)
        delta = timedelta(days=amount) if unit.startswith(("дн", "ден")) else \
            timedelta(hours=amount) if unit.startswith("ч") else timedelta(minutes=amount)
        return now + delta

    # a time is "15:00", or "в 15.00" -- a bare "2.10" is a date, never a time
    time_m = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", text) or re.search(r"в\s+(\d{1,2})\.(\d{2})(?![.\d])", text)

    def with_time(day: datetime) -> datetime:
        if time_m and int(time_m.group(1)) < 24 and int(time_m.group(2)) < 60:
            return day.replace(hour=int(time_m.group(1)), minute=int(time_m.group(2)), second=0, microsecond=0)
        return day.replace(hour=0, minute=0, second=0, microsecond=0)

    for word, shift in _RELATIVE_DAYS.items():
        if re.search(rf"(?<!\w){word}(?!\w)", text):
            return with_time(now + timedelta(days=shift))

    try:                                   # ISO first: dayfirst must never touch it
        return datetime.fromisoformat(text.replace(" в ", " ").strip())
    except ValueError:
        pass

    if re.fullmatch(r"(?:в\s*)?\d{1,2}:\d{2}", text) or re.fullmatch(r"в\s*\d{1,2}\.\d{2}", text):   # only a time: today, or tomorrow if it's passed
        t = with_time(now)
        return t if t > now else t + timedelta(days=1)

    m = re.search(r"(?<![\d:])(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?![\d:])", text)
    if m:
        year = int(m.group(3)) if m.group(3) else now.year
        if year < 100:
            year += 2000
        try:
            day = datetime(year, int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
        return with_time(day)

    try:
        from dateutil import parser as dateutil_parser

        return dateutil_parser.parse(value, dayfirst=True, fuzzy=True)
    except Exception:
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


def _event_label(e: dict) -> str:
    return f"«{e['title']}» {datetime.fromisoformat(e['start']).strftime('%d.%m %H:%M')}"


@register_impl("delete_event")
@log_call("delete_event")
async def _delete_event(*, query: str = "", date_str: str = "", delete_all: bool = False) -> dict:
    q = query.strip().lower()
    day = _parse_datetime(date_str) if date_str.strip() else None
    if date_str.strip() and day is None:
        return {"status": "error", "message": f"Не смог разобрать дату «{date_str}»."}
    if not q and day is None:
        return {"status": "error", "message": "Скажите, какое событие удалить: название или день."}

    def _matches(e: dict) -> bool:
        try:
            start = datetime.fromisoformat(e["start"])
        except (KeyError, ValueError):
            return False
        if day is not None and start.date() != day.date():
            return False
        return not q or q in e.get("title", "").lower() or e.get("id") == q

    def _mutate(data: list) -> tuple[list, tuple[list, list]]:
        hits = sorted((e for e in data if _matches(e)), key=lambda e: e["start"])
        if len(hits) > 1 and not delete_all:
            return data, ([], hits)          # ambiguous: delete nothing, ask which one
        return [e for e in data if e not in hits], (hits, [])

    removed, ambiguous = await _events_store.mutate(_mutate)
    if ambiguous:
        listing = "; ".join(_event_label(e) for e in ambiguous[:8])
        return {"status": "ambiguous", "message": (
            f"Подходит несколько событий: {listing}. Уточните, какое удалить, или скажите «удали все».")}
    if not removed:
        what = f"«{query}»" if q else "на этот день"
        return {"status": "not_found", "message": f"Не нашёл событий {what}."}
    names = ", ".join(_event_label(e) for e in removed)
    return {"status": "ok", "message": f"Удалил из календаря: {names}.", "removed": removed}


@register_tool
@function_tool
async def delete_event(context: RunContext, query: str = "", date_str: str = "", delete_all: bool = False) -> str:
    """Delete an event from the local calendar ("удали встречу с Егором",
    "отмени CS на сегодня", "удали все события на завтра"). If several events
    match, nothing is deleted and the matches are returned -- ask the user
    which one, then call again with a more specific query or date. Only pass
    delete_all=True when the user clearly asked to remove all of them.

    Args:
        query: Part of the event's title, e.g. "Егор" or "CS". Empty = any title.
        date_str: Optional day to narrow it down: "сегодня", "завтра", "2026-10-02".
        delete_all: True to delete every matching event at once.
    """
    result = await _delete_event(query=query, date_str=date_str, delete_all=delete_all)
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
                # promo-plan steps (tools/content_plan.py) always go to Telegram too: the owner wants them on the phone
                if not runtime.user_present() or reminder.get("telegram"):
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
    named = f" «{label}»" if label else ""
    return {"status": "ok", "message": f"Таймер{named} на {minutes:g} минут запущен."}


@register_tool
@function_tool
async def set_timer(context: RunContext, minutes: float, label: str = "") -> str:
    """Start a countdown timer that announces itself out loud when it ends.
    Several can run at once; give each a label if the user named it.

    Args:
        minutes: Timer duration in minutes (0.5 = 30 seconds).
        label: Optional name, e.g. "паста" or "стирка".
    """
    result = await _set_timer(minutes=minutes, label=label)
    return result["message"]


def _left(at: datetime, now: datetime) -> str:
    secs = max(0, int((at - now).total_seconds()))
    if secs < 3600:
        return f"через {secs // 60} мин {secs % 60} с"
    if at.date() == now.date():
        return f"в {at.strftime('%H:%M')}"
    return at.strftime("%d.%m %H:%M")


@register_impl("list_reminders")
@log_call("list_reminders")
async def _list_reminders() -> dict:
    now = datetime.now()
    items = sorted(await _reminders_store.load(), key=lambda r: r.get("at", ""))
    if not items:
        return {"status": "ok", "message": "Активных таймеров и напоминаний нет.", "reminders": []}
    lines = []
    for r in items:
        kind = "таймер" if r.get("kind") == "timer" else "напоминание"
        lines.append(f"{kind} «{r.get('text', '')}» — {_left(datetime.fromisoformat(r['at']), now)}")
    return {"status": "ok", "message": "; ".join(lines) + ".", "reminders": items}


@register_tool
@function_tool
async def list_reminders(context: RunContext) -> str:
    """List active timers and reminders with the time left ("сколько осталось на таймере?")."""
    result = await _list_reminders()
    return result["message"]


@register_impl("cancel_reminder")
@log_call("cancel_reminder")
async def _cancel_reminder(*, query: str = "") -> dict:
    q = query.strip().lower()

    def _mutate(data: list) -> tuple[list, list]:
        if q in ("", "все", "всё", "all"):
            return [], data
        if q in ("таймер", "timer"):
            hit = [r for r in data if r.get("kind") == "timer"][-1:]
        else:
            hit = [r for r in data if q in r.get("text", "").lower() or r.get("id") == q][:1]
        return [r for r in data if r not in hit], hit

    removed = await _reminders_store.mutate(_mutate)
    if not removed:
        return {"status": "not_found", "message": f"Не нашёл таймер или напоминание «{query}»."}
    names = ", ".join(f"«{r.get('text', '')}»" for r in removed)
    return {"status": "ok", "message": f"Отменил: {names}."}


@register_tool
@function_tool
async def cancel_reminder(context: RunContext, query: str = "") -> str:
    """Cancel a timer or reminder by part of its text/label. "таймер" cancels the
    latest timer; empty or "все" cancels everything.

    Args:
        query: Text identifying which one, e.g. "паста"; "все" for all.
    """
    result = await _cancel_reminder(query=query)
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
