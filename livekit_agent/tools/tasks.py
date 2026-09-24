"""Background tasks: multi-step goals Jarvis works on by itself.

"Разбери загрузки и напиши мне в Телеграм, что там было" shouldn't hold the
conversation hostage for minutes, and shouldn't die with a restart. A task
is a record in config.TASKS_FILE; `task_runner_loop()` (voice worker,
console mode) picks queued tasks one at a time and runs each through
agent_loop with the same tools the voice agent has.

Nobody is watching a background task, so every tool call goes through
policy.py: safe calls run, side-effecting ones need the owner's "да" on
Telegram (approvals.py) unless the user explicitly trusted the task, and
task/trigger management is never done from inside a task. The daily budget
(usage.py) is checked before the task and before every model call.

When a task ends, the result is spoken if someone is at the PC, and sent to
Telegram if not (or if the task came from Telegram in the first place).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime

from livekit.agents import RunContext, function_tool

import config
from tools import runtime
from tools._logging import log_call
from tools._store import JsonStore
from tools.registry import register_impl, register_tool

logger = logging.getLogger("jarvis-voice-agent.tasks")

_store = JsonStore(config.TASKS_FILE, default=[])
# Where tasks created in this process came from; telegram_bridge.py sets it to
# "telegram" so results of tasks the owner asked for in chat go back to chat.
DEFAULT_SOURCE = "voice"
_POLL_S = 3.0
_MAX_ATTEMPTS = 2
_KEEP_FINISHED = 50
_ACTIVE = ("queued", "running", "waiting_approval")

# Tools a background task may never call -- they'd let a task spawn tasks or
# rewrite Jarvis's own automation. Also stripped from the task's tool list so
# the model doesn't even try.
_EXCLUDED_TOOLS = frozenset({
    "start_task", "cancel_task", "list_tasks", "task_status",
    "create_trigger", "delete_trigger", "toggle_trigger", "list_triggers",
    "create_scenario", "edit_scenario", "delete_scenario",
})

TASK_PROMPT = """\
Ты — фоновый исполнитель голосового ассистента Джарвис на Windows-компьютере владельца.
Тебе дали задачу; выполни её ЦЕЛИКОМ с помощью инструментов, без разговоров.

ПРАВИЛА:
- Пользователя рядом нет, задавать вопросы некому. Если без человека никак (капча, код из SMS,
  непонятно, что именно имелось в виду, а ошибка дорогая) — остановись и ответь текстом,
  начинающимся с «НУЖЕН_ЧЕЛОВЕК:», и объясни, что нужно.
- Проверяй результат каждого важного шага (после открытия приложения — что окно появилось, после
  записи файла — что он есть). Не сработало — попробуй другой способ, но не повторяй одно и то же
  больше двух раз.
- Выбирай самый дешёвый подходящий инструмент: готовые инструменты (window_manager, file_manager,
  system_control, photoshop_control …) вместо use_computer; use_computer — только когда без
  кликов по экрану никак.
- Некоторые шаги требуют разрешения владельца — инструмент тогда вернёт отказ или задержится.
  Отказ = не делай этот шаг и не пытайся обойти его другим инструментом.
- Не выдумывай данные (почту, телефоны, пароли), которых нет в задаче или в памяти.
- В конце — короткий отчёт на русском, 1–3 предложения: что сделано и что важно знать.
"""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _match(tasks: list[dict], query: str) -> dict | None:
    q = query.strip().lower()
    for t in reversed(tasks):
        if t["id"] == q or (q and q in t["goal"].lower()):
            return t
    return None


async def _update(task_id: str, **fields) -> dict | None:
    def _mutate(data: list) -> tuple[list, dict | None]:
        for t in data:
            if t["id"] == task_id:
                t.update(fields)
                return data, dict(t)
        return data, None

    return await _store.mutate(_mutate)


async def _append_log(task_id: str, line: str) -> None:
    def _mutate(data: list) -> tuple[list, None]:
        for t in data:
            if t["id"] == task_id:
                t.setdefault("log", []).append(f"{time.strftime('%H:%M:%S')} {line}")
                t["log"] = t["log"][-40:]
        return data, None

    await _store.mutate(_mutate)


# ---------------------------------------------------------------------------
# LLM-facing tools
# ---------------------------------------------------------------------------


async def enqueue(goal: str, *, trusted: bool = False, source: str = "voice") -> dict:
    task = {
        "id": uuid.uuid4().hex[:6], "goal": goal.strip(), "status": "queued", "trusted": bool(trusted),
        "source": source, "created": _now(), "attempts": 0, "log": [], "result": "",
    }

    def _mutate(data: list) -> tuple[list, dict]:
        finished = [t for t in data if t["status"] not in _ACTIVE]
        active = [t for t in data if t["status"] in _ACTIVE]
        return finished[-_KEEP_FINISHED:] + active + [task], task

    return await _store.mutate(_mutate)


@register_impl("start_task")
@log_call("start_task")
async def _start_task(*, goal: str, trusted: bool = False, source: str | None = None) -> dict:
    if not goal.strip():
        return {"status": "error", "message": "Пустая задача."}
    import usage

    refusal = usage.check_budget("фоновую задачу")
    if refusal:
        return {"status": "error", "message": refusal}
    task = await enqueue(goal, trusted=trusted, source=source or DEFAULT_SOURCE)
    active = [t for t in await _store.load() if t["status"] in _ACTIVE]
    queue_note = "" if len(active) <= 1 else f" В очереди перед ней: {len(active) - 1}."
    return {"status": "ok", "message": f"Взял в работу в фоне (задача {task['id']}).{queue_note} "
                                        f"Сообщу, когда закончу.", "task_id": task["id"]}


@register_tool
@function_tool
async def start_task(context: RunContext, goal: str, trusted: bool = False) -> str:
    """Hand a multi-step job to Jarvis's background worker so the conversation
    isn't blocked while it runs. Use for anything that takes more than a
    couple of tool calls or should happen while the user does something else
    ("разбери загрузки", "найди и пришли мне …", "подготовь проект к …").
    The result is spoken when done, or sent to Telegram if the user is away.

    Args:
        goal: The complete task as a clear instruction with every detail
            needed (paths, names, what to report back) -- the worker can't ask
            follow-up questions.
        trusted: True ONLY if the user explicitly said to do it without asking
            for confirmations ("делай без подтверждений", "можно без спроса").
            Otherwise side-effect steps (sending messages, closing apps,
            working on the screen) wait for the owner's OK on Telegram.
    """
    result = await _start_task(goal=goal, trusted=trusted)
    return result["message"]


@register_impl("list_tasks")
@log_call("list_tasks")
async def _list_tasks() -> dict:
    tasks = await _store.load()
    if not tasks:
        return {"status": "ok", "message": "Фоновых задач нет."}
    labels = {"queued": "в очереди", "running": "выполняется", "waiting_approval": "ждёт вашего ответа в Telegram",
              "done": "готово", "failed": "не удалась", "cancelled": "отменена", "needs_human": "нужна ваша помощь"}
    recent = [t for t in tasks if t["status"] in _ACTIVE] or tasks[-5:]
    listing = "; ".join(f"{t['id']} «{t['goal'][:60]}» — {labels.get(t['status'], t['status'])}" for t in recent)
    return {"status": "ok", "message": f"Задачи: {listing}."}


@register_tool
@function_tool
async def list_tasks(context: RunContext) -> str:
    """List background tasks: the active ones, or the last few if none are active."""
    return (await _list_tasks())["message"]


@register_impl("task_status")
@log_call("task_status")
async def _task_status(*, query: str) -> dict:
    task = _match(await _store.load(), query)
    if task is None:
        return {"status": "not_found", "message": f"Не нашёл задачу «{query}»."}
    last = task.get("log", [])[-3:]
    detail = task.get("result") or ("; ".join(last) if last else "ещё не начата")
    return {"status": "ok", "message": f"Задача «{task['goal'][:80]}»: {task['status']}. {detail}"}


@register_tool
@function_tool
async def task_status(context: RunContext, query: str) -> str:
    """What a background task is doing now / how it ended.

    Args:
        query: The task id or a word from its description.
    """
    return (await _task_status(query=query))["message"]


@register_impl("cancel_task")
@log_call("cancel_task")
async def _cancel_task(*, query: str) -> dict:
    tasks = await _store.load()
    task = _match([t for t in tasks if t["status"] in _ACTIVE], query)
    if task is None:
        return {"status": "not_found", "message": f"Нет активной задачи «{query}»."}
    await _update(task["id"], status="cancelled", finished=_now(), result="Отменена пользователем.")
    return {"status": "ok", "message": f"Отменил задачу «{task['goal'][:80]}»."}


@register_tool
@function_tool
async def cancel_task(context: RunContext, query: str) -> str:
    """Cancel a queued or running background task.

    Args:
        query: The task id or a word from its description.
    """
    return (await _cancel_task(query=query))["message"]


# ---------------------------------------------------------------------------
# Runner (voice worker, console mode only)
# ---------------------------------------------------------------------------


async def _recover() -> None:
    """Tasks left "running" by a crash get one more go; past that they fail."""
    def _mutate(data: list) -> tuple[list, None]:
        for t in data:
            if t["status"] in ("running", "waiting_approval"):
                if t.get("attempts", 0) < _MAX_ATTEMPTS:
                    t["status"] = "queued"
                else:
                    t.update(status="failed", finished=_now(), result="Прервана перезапуском, повторять не стал.")
        return data, None

    await _store.mutate(_mutate)


async def _claim_next() -> dict | None:
    def _mutate(data: list) -> tuple[list, dict | None]:
        for t in data:
            if t["status"] == "queued":
                t.update(status="running", started=_now(), attempts=t.get("attempts", 0) + 1)
                return data, dict(t)
        return data, None

    return await _store.mutate(_mutate)


def _is_cancelled(task_id: str) -> bool:
    from tools._store import read_json

    for t in read_json(config.TASKS_FILE, []):
        if t["id"] == task_id:
            return t["status"] == "cancelled"
    return True


def _make_gate(task: dict):
    import approvals
    import policy
    import telegram_owner

    async def gate(tool: str, args: dict) -> str | None:
        level = policy.classify(tool, args)
        if level == policy.BLOCK:
            return "Это действие в фоновом режиме запрещено."
        if level == policy.SAFE or task["trusted"]:
            return None
        if telegram_owner.load_owner_id() is None:
            return ("Для этого шага нужно разрешение владельца, но Telegram не подключён — шаг пропущен. "
                    "Можно повторить задачу с пометкой «без подтверждений».")
        await _update(task["id"], status="waiting_approval")
        await _append_log(task["id"], f"жду разрешения: {policy.describe(tool, args)}")
        ok = await approvals.request_approval(
            f"Фоновая задача «{task['goal'][:100]}» хочет выполнить: {policy.describe(tool, args)}")
        if _is_cancelled(task["id"]):
            return "Задача отменена."
        await _update(task["id"], status="running")
        await _append_log(task["id"], "разрешено" if ok else "запрещено владельцем")
        return None if ok else "Владелец не разрешил этот шаг."

    return gate


async def _report(task: dict, status: str, text: str) -> None:
    import notify

    if status == "done":
        spoken = f"Готово: {text}"
    elif status == "needs_human":
        spoken = f"По задаче «{task['goal'][:60]}» нужна ваша помощь: {text.removeprefix('НУЖЕН_ЧЕЛОВЕК:').strip()}"
    elif status == "cancelled":
        return
    else:
        spoken = f"Не получилось выполнить «{task['goal'][:60]}»: {text}"
    if task.get("source") == "telegram" or not runtime.user_present():
        notify.notify_owner(f"🗂 {spoken}", kind="task")
    await runtime.say(spoken)


async def run_task(task: dict) -> tuple[str, str]:
    """Runs one claimed task to completion -> (status, result text)."""
    import agent_loop
    import usage
    from tools.memory import format_memory_for_prompt, load_memory

    refusal = usage.check_budget("фоновую задачу")
    if refusal:
        return "failed", refusal

    def should_stop() -> bool:
        left = usage.budget_left()
        return _is_cancelled(task["id"]) or (left is not None and left <= 0)

    def on_step(line: str) -> None:
        asyncio.get_running_loop().create_task(_append_log(task["id"], line))

    memory_block = format_memory_for_prompt(await load_memory())
    system_text = TASK_PROMPT + (f"\n{memory_block}" if memory_block else "")
    text, _ = await agent_loop.run(
        system_text=system_text,
        tools_param=agent_loop.tool_schemas(exclude=_EXCLUDED_TOOLS),
        history=[],
        # The time goes in the user turn, not the system prompt, so the
        # cached prefix (system + tools) stays byte-identical across tasks.
        user_text=f"Сейчас {datetime.now().strftime('%Y-%m-%d %H:%M, %A')}.\nЗадача: {task['goal']}",
        model=config.TASK_MODEL,
        max_steps=config.TASK_MAX_STEPS,
        source="task",
        gate=_make_gate(task),
        should_stop=should_stop,
        on_step=on_step,
    )
    if _is_cancelled(task["id"]):
        return "cancelled", "Отменена."
    if text.startswith("НУЖЕН_ЧЕЛОВЕК"):
        return "needs_human", text
    if text == "Остановлено.":
        return "failed", "Остановлена: исчерпан дневной лимит расходов."
    return "done", text


async def task_runner_loop() -> None:
    await _recover()
    while True:
        task = None
        try:
            task = await _claim_next()
            if task is None:
                await asyncio.sleep(_POLL_S)
                continue
            logger.info("running task %s: %s", task["id"], task["goal"])
            try:
                status, text = await run_task(task)
            except Exception as exc:
                logger.exception("task %s crashed", task["id"])
                status, text = "failed", f"внутренняя ошибка: {exc}"
            if not _is_cancelled(task["id"]):
                await _update(task["id"], status=status, result=text, finished=_now())
            await _report(task, status, text)
        except Exception:
            logger.exception("task runner tick failed")
            await asyncio.sleep(_POLL_S)
