"""Triggers: things Jarvis does on its own when a time or an event comes.

A trigger is "when X, do Y", stored in config.TRIGGERS_FILE:

  when  daily        "09:00", "09:00 будни", "21:30 выходные"
        interval     minutes, e.g. "60"
        app_start    process name part, e.g. "discord"
        app_exit     same, fires when the last matching process is gone
        file_new     folder, optionally "|*.pdf" -- a new file appeared there
        idle         minutes without keyboard/mouse input
        battery_low  percent
        startup      (no value) -- once each time Jarvis starts

  do    scenario     run a saved scenario by name (no LLM -- free)
        task         start a background task (tasks.py); "{detail}" in the
                     text is replaced by what fired it (the new file, the app)
        say          speak the text (and send it to Telegram if nobody's at the PC)
        briefing     speak the morning briefing (tools/briefing.py, no LLM -- free)

Detection is plain polling (psutil, os.scandir, GetLastInputInfo) every few
seconds, no LLM. Creating/deleting a trigger needs the same spoken two-step
confirmation as scenarios -- it makes Jarvis act on its own later.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

from livekit.agents import RunContext, function_tool

import config
from tools import runtime
from tools._logging import log_call
from tools._store import JsonStore
from tools.registry import IMPL_REGISTRY, register_impl, register_tool

logger = logging.getLogger("jarvis-voice-agent.triggers")

_store = JsonStore(config.TRIGGERS_FILE, default=[])
_POLL_S = 10.0
_MIN_GAP_S = 60.0          # a trigger never fires more often than this
_DAILY_GRACE_S = 3600.0    # a daily trigger missed by up to an hour (PC was off) still fires

WhenKind = Literal["daily", "interval", "app_start", "app_exit", "file_new", "idle", "battery_low", "startup"]
ActionKind = Literal["scenario", "task", "say", "briefing"]
_WEEKDAYS, _WEEKEND = [0, 1, 2, 3, 4], [5, 6]


# ---------------------------------------------------------------------------
# Parsing a spoken "when" into a spec
# ---------------------------------------------------------------------------


def parse_when(kind: str, value: str) -> dict | str:
    """-> spec dict, or an error message."""
    value = (value or "").strip()
    if kind == "daily":
        m = re.search(r"(\d{1,2})[:.](\d{2})", value)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            return "Для ежедневного триггера нужно время в формате ЧЧ:ММ."
        low = value.lower()
        days = _WEEKDAYS if "будн" in low else _WEEKEND if "выходн" in low else list(range(7))
        return {"time": f"{int(m.group(1)):02d}:{m.group(2)}", "days": days}
    if kind in ("interval", "idle"):
        m = re.search(r"\d+", value)
        if not m or int(m.group()) < (5 if kind == "interval" else 1):
            return "Нужно число минут (для интервала — не меньше 5)."
        return {"minutes": int(m.group())}
    if kind == "battery_low":
        m = re.search(r"\d+", value)
        return {"percent": int(m.group()) if m else 20}
    if kind in ("app_start", "app_exit"):
        if len(value) < 3:
            return "Нужно название процесса (хотя бы 3 буквы), например discord."
        return {"process": value.lower().removesuffix(".exe")}
    if kind == "file_new":
        folder, _, pattern = value.partition("|")
        path = Path(os.path.expandvars(folder.strip())).expanduser()
        if not path.is_dir():
            aliases = {"загрузки": "Downloads", "downloads": "Downloads", "рабочий стол": "Desktop",
                       "desktop": "Desktop", "документы": "Documents", "documents": "Documents"}
            alias = aliases.get(folder.strip().lower())
            path = Path.home() / alias if alias else path
        if not path.is_dir():
            return f"Папка «{folder}» не найдена."
        return {"folder": str(path), "pattern": pattern.strip() or "*"}
    if kind == "startup":
        return {}
    return f"Неизвестный тип триггера «{kind}»."


def describe(trigger: dict) -> str:
    kind, spec = trigger["when"], trigger["spec"]
    when = {
        "daily": lambda: f"каждый день в {spec['time']}" + (
            " по будням" if spec.get("days") == _WEEKDAYS else " по выходным" if spec.get("days") == _WEEKEND else ""),
        "interval": lambda: f"каждые {spec['minutes']} мин",
        "app_start": lambda: f"когда запускается {spec['process']}",
        "app_exit": lambda: f"когда закрывается {spec['process']}",
        "file_new": lambda: f"когда в {spec['folder']} появляется файл {spec['pattern']}",
        "idle": lambda: f"когда компьютер простаивает {spec['minutes']} мин",
        "battery_low": lambda: f"когда заряд ниже {spec['percent']}%",
        "startup": lambda: "при запуске Джарвиса",
    }[kind]()
    action = {"scenario": "запустить сценарий", "task": "выполнить задачу", "say": "сказать",
              "briefing": "рассказать сводку"}[trigger["action"]]
    trust = " (без подтверждений)" if trigger.get("trusted") else ""
    return f"«{trigger['name']}»: {when} — {action} «{trigger['action_value']}»{trust}"


# ---------------------------------------------------------------------------
# Condition checks (pure where possible, so they can be unit-tested)
# ---------------------------------------------------------------------------


def daily_due(spec: dict, last_fired: float, now: datetime) -> bool:
    hh, mm = map(int, spec["time"].split(":"))
    scheduled = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now < scheduled or now.weekday() not in spec.get("days", range(7)):
        return False
    if (now - scheduled).total_seconds() > _DAILY_GRACE_S:
        return False
    return last_fired < scheduled.timestamp()


def _idle_seconds() -> float:
    if config.SYSTEM != "Windows":
        return 0.0
    import ctypes

    class _LastInput(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = _LastInput()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):  # type: ignore[attr-defined]
        return 0.0
    return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0  # type: ignore[attr-defined]


def _running_processes() -> set[str]:
    import psutil

    names = set()
    for proc in psutil.process_iter(["name"]):
        name = (proc.info.get("name") or "").lower()
        if name:
            names.add(name.removesuffix(".exe"))
    return names


def _list_folder(folder: str, pattern: str) -> set[str]:
    try:
        return {e.name for e in os.scandir(folder)
                if e.is_file() and fnmatch.fnmatch(e.name.lower(), pattern.lower())
                and not e.name.endswith((".crdownload", ".part", ".tmp"))}
    except OSError:
        return set()


class _Watcher:
    """Per-process edge-detection state (what was true on the previous tick)."""

    def __init__(self) -> None:
        self.started_at = time.time()
        self.fired_startup: set[str] = set()
        self.app_seen: dict[str, bool] = {}
        self.files_seen: dict[str, set[str]] = {}
        self.latched: dict[str, bool] = {}  # idle / battery: fire once per episode

    def check(self, trigger: dict, now: datetime, procs: set[str] | None) -> str | None:
        """-> a short "what happened" detail if the trigger fires now, else None."""
        tid, kind, spec = trigger["id"], trigger["when"], trigger["spec"]
        last = float(trigger.get("last_fired", 0))
        if time.time() - last < _MIN_GAP_S and kind != "startup":
            return None
        if kind == "daily":
            return spec["time"] if daily_due(spec, last, now) else None
        if kind == "interval":
            base = last or float(trigger.get("created_ts", self.started_at))
            return "интервал" if time.time() - base >= spec["minutes"] * 60 else None
        if kind == "startup":
            if tid in self.fired_startup:
                return None
            self.fired_startup.add(tid)
            return "запуск"
        if kind in ("app_start", "app_exit"):
            if procs is None:
                return None
            running = any(spec["process"] in p for p in procs)
            before = self.app_seen.get(tid)
            self.app_seen[tid] = running
            if before is None:
                return None  # first look: just establish the baseline
            if kind == "app_start" and running and not before:
                return spec["process"]
            if kind == "app_exit" and before and not running:
                return spec["process"]
            return None
        if kind == "file_new":
            current = _list_folder(spec["folder"], spec["pattern"])
            before = self.files_seen.get(tid)
            self.files_seen[tid] = current
            if before is None:
                return None
            new = sorted(current - before)
            return str(Path(spec["folder"]) / new[0]) if new else None
        if kind == "idle":
            idle = _idle_seconds() >= spec["minutes"] * 60
            return self._latch(tid, idle, f"простой {spec['minutes']} мин")
        if kind == "battery_low":
            import psutil

            battery = psutil.sensors_battery()
            low = battery is not None and not battery.power_plugged and battery.percent < spec["percent"]
            return self._latch(tid, low, f"заряд {battery.percent:.0f}%" if battery else "")
        return None

    def _latch(self, tid: str, condition: bool, detail: str) -> str | None:
        was = self.latched.get(tid, False)
        self.latched[tid] = condition
        return detail if condition and not was else None


# ---------------------------------------------------------------------------
# Firing
# ---------------------------------------------------------------------------


async def fire(trigger: dict, detail: str) -> None:
    import notify

    action, value = trigger["action"], trigger["action_value"]
    logger.info("trigger %s fired (%s): %s %s", trigger["name"], detail, action, value)

    def _mutate(data: list) -> tuple[list, None]:
        for t in data:
            if t["id"] == trigger["id"]:
                t["last_fired"] = time.time()
                t["fire_count"] = t.get("fire_count", 0) + 1
        return data, None

    await _store.mutate(_mutate)

    if action == "scenario":
        impl = IMPL_REGISTRY.get("run_scenario")
        if impl is not None:
            result = await impl(name_or_trigger=value)
            if result.get("status") != "ok" and not runtime.user_present():
                notify.notify_owner(f"⚙️ Триггер «{trigger['name']}»: {result.get('message')}", kind="trigger")
    elif action == "task":
        from tools import tasks

        goal = value.replace("{detail}", detail)
        await tasks.enqueue(f"{goal}\n(Запущено триггером «{trigger['name']}»: {detail}.)",
                            trusted=bool(trigger.get("trusted")), source="trigger")
    elif action == "say":
        text = value.replace("{detail}", detail)
        if not runtime.user_present():
            notify.notify_owner(f"🔔 {text}", kind="trigger")
        await runtime.say(text)
    elif action == "briefing":
        impl = IMPL_REGISTRY.get("morning_briefing")
        if impl is not None:
            result = await impl(city="" if value in ("-", "сводка") else value)
            if not runtime.user_present():
                notify.notify_owner(f"☀️ {result.get('message', '')}", kind="trigger")
            await runtime.say(result.get("message", ""))


async def trigger_loop() -> None:
    watcher = _Watcher()
    while True:
        try:
            triggers = [t for t in await _store.load() if t.get("enabled", True)]
            needs_procs = any(t["when"] in ("app_start", "app_exit") for t in triggers)
            procs = await asyncio.to_thread(_running_processes) if needs_procs else None
            now = datetime.now()
            for trigger in triggers:
                try:
                    detail = await asyncio.to_thread(watcher.check, trigger, now, procs)
                    if detail is not None:
                        await fire(trigger, detail)
                except Exception:
                    logger.exception("trigger %s failed", trigger.get("name"))
        except Exception:
            logger.exception("trigger loop tick failed")
        await asyncio.sleep(_POLL_S)


# ---------------------------------------------------------------------------
# LLM-facing tools
# ---------------------------------------------------------------------------


def _find(data: list, name: str) -> dict | None:
    q = name.strip().lower()
    return next((t for t in data if t["name"].lower() == q or t["id"] == q), None)


@register_impl("create_trigger")
@log_call("create_trigger")
async def _create_trigger(*, name: str, when: str, when_value: str = "", action: str, action_value: str,
                          trusted: bool = False, confirm: bool = False) -> dict:
    spec = parse_when(when, when_value)
    if isinstance(spec, str):
        return {"status": "error", "message": spec}
    if action == "briefing" and not action_value.strip():
        action_value = "сводка"
    if action not in ("scenario", "task", "say", "briefing") or not action_value.strip():
        return {"status": "error", "message": "Действие должно быть scenario, task, say или briefing, с непустым значением."}
    trigger = {"id": uuid.uuid4().hex[:6], "name": name.strip(), "when": when, "spec": spec,
               "action": action, "action_value": action_value.strip(), "trusted": bool(trusted),
               "enabled": True, "created_ts": time.time(), "last_fired": 0}
    if action == "task" and not trusted:
        note = " Шаги с последствиями будут ждать вашего «да» в Telegram."
    else:
        note = ""
    if not confirm:
        return {"status": "needs_confirmation",
                "message": f"Создам триггер {describe(trigger)}.{note} Скажите «да, подтверждаю»."}

    def _mutate(data: list) -> tuple[list, None]:
        data = [t for t in data if t["name"].lower() != trigger["name"].lower()]
        data.append(trigger)
        return data, None

    await _store.mutate(_mutate)
    return {"status": "ok", "message": f"Триггер создан: {describe(trigger)}."}


@register_tool
@function_tool
async def create_trigger(
    context: RunContext,
    name: str,
    when: WhenKind,
    action: ActionKind,
    action_value: str,
    when_value: str = "",
    trusted: bool = False,
    confirm: bool = False,
) -> str:
    """Make Jarvis do something on its own when a time or an event comes
    ("каждое утро в 9 …", "когда в загрузках появится PDF …", "когда запускаю
    Discord …", "если я отошёл на 30 минут …"). Two-step like scenarios: call
    without confirm, read back the plan, and only after the user says
    «да, подтверждаю» call again with confirm=True.

    Args:
        name: Short name for the trigger.
        when: daily | interval | app_start | app_exit | file_new | idle | battery_low | startup.
        when_value: daily: "HH:MM", optionally with "будни"/"выходные"; interval/idle:
            minutes; app_start/app_exit: process name part (e.g. "discord");
            file_new: folder path or "загрузки"/"рабочий стол"/"документы",
            optionally "|*.pdf"; battery_low: percent; startup: empty.
        action: scenario (run a saved scenario, free) | task (background task,
            uses the LLM) | say (speak/send a text) | briefing (morning summary:
            weather, schedule, todos, news -- free, no LLM).
        action_value: Scenario name, full task instruction, the text to say, or
            for briefing a city (or "сводка" for the home city).
            In a task/say text, {detail} is replaced by what fired it (e.g. the
            new file's path).
        trusted: True only if the user explicitly said the task may act without
            asking each time.
        confirm: True only after the user verbally confirmed.
    """
    result = await _create_trigger(name=name, when=when, when_value=when_value, action=action,
                                   action_value=action_value, trusted=trusted, confirm=confirm)
    return result["message"]


@register_impl("list_triggers")
@log_call("list_triggers")
async def _list_triggers() -> dict:
    data = await _store.load()
    if not data:
        return {"status": "ok", "message": "Триггеров нет."}
    listing = "; ".join(describe(t) + ("" if t.get("enabled", True) else " [выключен]") for t in data)
    return {"status": "ok", "message": f"Триггеры: {listing}."}


@register_tool
@function_tool
async def list_triggers(context: RunContext) -> str:
    """List all triggers (automatic actions on time/events)."""
    return (await _list_triggers())["message"]


@register_impl("delete_trigger")
@log_call("delete_trigger")
async def _delete_trigger(*, name: str, confirm: bool = False) -> dict:
    data = await _store.load()
    trigger = _find(data, name)
    if trigger is None:
        return {"status": "not_found", "message": f"Триггер «{name}» не найден."}
    if not confirm:
        return {"status": "needs_confirmation",
                "message": f"Удалить триггер {describe(trigger)}? Скажите «да, подтверждаю»."}
    await _store.mutate(lambda d: ([t for t in d if t["id"] != trigger["id"]], None))
    return {"status": "ok", "message": f"Триггер «{trigger['name']}» удалён."}


@register_tool
@function_tool
async def delete_trigger(context: RunContext, name: str, confirm: bool = False) -> str:
    """Delete a trigger. Requires spoken confirmation (confirm=True on the second call).

    Args:
        name: The trigger's name.
        confirm: True only after the user verbally confirmed.
    """
    return (await _delete_trigger(name=name, confirm=confirm))["message"]


@register_impl("toggle_trigger")
@log_call("toggle_trigger")
async def _toggle_trigger(*, name: str, enabled: bool) -> dict:
    def _mutate(data: list) -> tuple[list, dict | None]:
        trigger = _find(data, name)
        if trigger is not None:
            trigger["enabled"] = bool(enabled)
        return data, trigger

    trigger = await _store.mutate(_mutate)
    if trigger is None:
        return {"status": "not_found", "message": f"Триггер «{name}» не найден."}
    return {"status": "ok", "message": f"Триггер «{trigger['name']}» {'включён' if enabled else 'выключен'}."}


@register_tool
@function_tool
async def toggle_trigger(context: RunContext, name: str, enabled: bool) -> str:
    """Turn a trigger on or off without deleting it.

    Args:
        name: The trigger's name.
        enabled: True to turn on, False to turn off.
    """
    return (await _toggle_trigger(name=name, enabled=enabled))["message"]
