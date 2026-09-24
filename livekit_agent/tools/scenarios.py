"""Scenario tools: record a voice macro as a list of tool calls, replay it later.

Design notes:
  * `create_scenario`/`edit_scenario` never write to disk on the first call.
    They validate the steps and hand back a spoken read-back of the plan; the
    system prompt instructs the LLM to only call them again with
    `confirm=True` after the user has said something like "да, подтверждаю".
    This keeps the "repeat back what you understood, then confirm" rule in
    Python rather than trusting the LLM to remember to ask.
  * `run_scenario` calls `tools.registry.IMPL_REGISTRY` directly -- it never
    goes back through the LLM, so replaying a scenario is deterministic and
    still gets logged by the same `log_call` decorators as a live tool call.
  * A scenario cannot contain scenario tools itself (no create/edit/delete/run
    nested inside a scenario), which rules out self-modifying or recursive
    scenarios outright.
"""

from __future__ import annotations

import json
from typing import Any

from livekit.agents import RunContext, function_tool

import config
from tools import runtime
from tools._logging import log_call
from tools._store import JsonStore
from tools.registry import IMPL_REGISTRY, register_impl, register_tool

_scenarios_store = JsonStore(config.SCENARIOS_FILE, default={})

_SCENARIO_TOOL_NAMES = {"create_scenario", "edit_scenario", "delete_scenario", "run_scenario"}


def _validate_steps(steps: list[dict[str, Any]]) -> tuple[bool, str, list[dict[str, Any]]]:
    if not steps:
        return False, "Сценарий должен содержать хотя бы один шаг.", []

    normalized: list[dict[str, Any]] = []
    for i, step in enumerate(steps, start=1):
        if not isinstance(step, dict) or "tool" not in step:
            return False, f"Шаг {i} должен быть объектом с полем «tool».", []

        tool = step["tool"]
        args = step.get("args") or {}
        if not isinstance(args, dict):
            return False, f"Шаг {i}: «args» должен быть объектом.", []

        if tool in _SCENARIO_TOOL_NAMES:
            return False, f"Шаг {i}: сценарий не может вызывать другой сценарий ({tool}).", []

        if tool not in IMPL_REGISTRY:
            available = ", ".join(sorted(IMPL_REGISTRY))
            return False, f"Шаг {i}: неизвестный инструмент «{tool}». Доступные: {available}.", []

        if tool == "run_predefined_script":
            script_name = args.get("script_name")
            if script_name not in config.SCRIPT_WHITELIST:
                return False, f"Шаг {i}: скрипт «{script_name}» не в белом списке.", []

        if tool == "system_control":
            action = args.get("action")
            if action not in config.SAFE_SYSTEM_ACTIONS:
                return False, f"Шаг {i}: системное действие «{action}» не разрешено.", []

        normalized.append({"tool": tool, "args": args})

    return True, "", normalized


def _format_steps(steps: list[dict[str, Any]]) -> str:
    lines = []
    for i, step in enumerate(steps, start=1):
        args = step.get("args") or {}
        args_str = ", ".join(f"{k}={v}" for k, v in args.items())
        lines.append(f"{i}. {step['tool']}({args_str})" if args_str else f"{i}. {step['tool']}()")
    return "; ".join(lines)


# ---------------------------------------------------------------------------
# create_scenario
# ---------------------------------------------------------------------------


@register_impl("create_scenario")
@log_call("create_scenario")
async def _create_scenario(
    *, name: str, trigger_phrase: str, steps: list[dict[str, Any]], confirm: bool = False
) -> dict:
    ok, error, normalized = _validate_steps(steps)
    if not ok:
        return {"status": "error", "message": error}

    plan = _format_steps(normalized)

    if not confirm:
        return {
            "status": "needs_confirmation",
            "message": (
                f"Записал сценарий «{name}» с фразой-триггером «{trigger_phrase}»: {plan}. "
                "Скажите «да, подтверждаю», чтобы сохранить."
            ),
        }

    scenario = {"trigger_phrase": trigger_phrase, "steps": normalized}

    def _mutate(data: dict) -> tuple[dict, None]:
        data[name] = scenario
        return data, None

    await _scenarios_store.mutate(_mutate)
    return {"status": "ok", "message": f"Сценарий «{name}» сохранён."}


@register_tool
@function_tool
async def create_scenario(
    context: RunContext,
    name: str,
    trigger_phrase: str,
    steps_json: str,
    confirm: bool = False,
) -> str:
    """Create a saved voice scenario: a named sequence of tool calls triggered by a phrase.

    Break the user's spoken description into an ordered list of steps, each
    `{"tool": "<name of one of the other available tools>", "args": {...}}`,
    and pass the whole list as a JSON-encoded string in `steps_json`
    (e.g. '[{"tool": "open_application", "args": {"name": "VS Code"}}]').
    Call this WITHOUT confirm first -- you'll get back a spoken read-back of
    the recorded plan. Only call it again with confirm=True after the user
    has verbally confirmed (e.g. said "да, подтверждаю"). Never skip the
    read-back.

    Args:
        name: Unique, human-readable scenario name, e.g. "рабочий режим".
        trigger_phrase: The voice phrase that should later trigger this scenario.
        steps_json: JSON-encoded ordered list of {"tool": ..., "args": {...}} steps.
        confirm: Set True only after the user has verbally confirmed the read-back plan.
    """
    try:
        steps = json.loads(steps_json)
    except json.JSONDecodeError as exc:
        return f"Не смог разобрать steps_json как JSON: {exc}"
    result = await _create_scenario(name=name, trigger_phrase=trigger_phrase, steps=steps, confirm=confirm)
    return result["message"]


# ---------------------------------------------------------------------------
# edit_scenario
# ---------------------------------------------------------------------------


@register_impl("edit_scenario")
@log_call("edit_scenario")
async def _edit_scenario(
    *,
    name: str,
    trigger_phrase: str | None = None,
    steps: list[dict[str, Any]] | None = None,
    confirm: bool = False,
) -> dict:
    existing = await _scenarios_store.load()
    if name not in existing:
        return {"status": "not_found", "message": f"Сценарий «{name}» не найден."}

    current = existing[name]
    new_trigger = trigger_phrase or current["trigger_phrase"]
    new_steps = current["steps"]

    if steps is not None:
        ok, error, normalized = _validate_steps(steps)
        if not ok:
            return {"status": "error", "message": error}
        new_steps = normalized

    plan = _format_steps(new_steps)

    if not confirm:
        return {
            "status": "needs_confirmation",
            "message": (
                f"Обновляю сценарий «{name}»: фраза «{new_trigger}», шаги: {plan}. "
                "Скажите «да, подтверждаю», чтобы сохранить изменения."
            ),
        }

    def _mutate(data: dict) -> tuple[dict, None]:
        data[name] = {"trigger_phrase": new_trigger, "steps": new_steps}
        return data, None

    await _scenarios_store.mutate(_mutate)
    return {"status": "ok", "message": f"Сценарий «{name}» обновлён."}


@register_tool
@function_tool
async def edit_scenario(
    context: RunContext,
    name: str,
    trigger_phrase: str | None = None,
    steps_json: str | None = None,
    confirm: bool = False,
) -> str:
    """Edit an existing scenario's trigger phrase and/or steps.

    Same confirm-then-save pattern as create_scenario: call without confirm
    to get a spoken read-back of the resulting scenario, then again with
    confirm=True only after the user verbally confirms.

    Args:
        name: The existing scenario's name.
        trigger_phrase: New trigger phrase, or omit to keep the current one.
        steps_json: New ordered list of {"tool": ..., "args": {...}} steps as a
            JSON-encoded string, or omit to keep the current steps.
        confirm: Set True only after the user has verbally confirmed the read-back plan.
    """
    steps = None
    if steps_json is not None:
        try:
            steps = json.loads(steps_json)
        except json.JSONDecodeError as exc:
            return f"Не смог разобрать steps_json как JSON: {exc}"
    result = await _edit_scenario(name=name, trigger_phrase=trigger_phrase, steps=steps, confirm=confirm)
    return result["message"]


# ---------------------------------------------------------------------------
# delete_scenario
# ---------------------------------------------------------------------------


@register_impl("delete_scenario")
@log_call("delete_scenario")
async def _delete_scenario(*, name: str, confirm: bool = False) -> dict:
    existing = await _scenarios_store.load()
    if name not in existing:
        return {"status": "not_found", "message": f"Сценарий «{name}» не найден."}

    if not confirm:
        return {
            "status": "needs_confirmation",
            "message": f"Удалить сценарий «{name}»? Это необратимо. Скажите «да, подтверждаю».",
        }

    def _mutate(data: dict) -> tuple[dict, None]:
        data.pop(name, None)
        return data, None

    await _scenarios_store.mutate(_mutate)
    return {"status": "ok", "message": f"Сценарий «{name}» удалён."}


@register_tool
@function_tool
async def delete_scenario(context: RunContext, name: str, confirm: bool = False) -> str:
    """Delete a saved scenario. Irreversible -- requires spoken confirmation.

    Args:
        name: The scenario's name to delete.
        confirm: Must be True, and only after the user has verbally confirmed.
    """
    result = await _delete_scenario(name=name, confirm=confirm)
    return result["message"]


# ---------------------------------------------------------------------------
# list_scenarios
# ---------------------------------------------------------------------------


@register_impl("list_scenarios")
@log_call("list_scenarios")
async def _list_scenarios() -> dict:
    existing = await _scenarios_store.load()
    if not existing:
        return {"status": "ok", "message": "Сохранённых сценариев пока нет."}

    listing = "; ".join(
        f"«{name}» (триггер «{s['trigger_phrase']}», {len(s['steps'])} шаг(ов))"
        for name, s in existing.items()
    )
    return {"status": "ok", "message": f"Сценарии: {listing}."}


@register_tool
@function_tool
async def list_scenarios(context: RunContext) -> str:
    """List all saved scenarios with their trigger phrase and step count."""
    result = await _list_scenarios()
    return result["message"]


# ---------------------------------------------------------------------------
# run_scenario
# ---------------------------------------------------------------------------


@register_impl("run_scenario")
@log_call("run_scenario")
async def _run_scenario(*, name_or_trigger: str) -> dict:
    existing = await _scenarios_store.load()

    match_name = None
    if name_or_trigger in existing:
        match_name = name_or_trigger
    else:
        q = name_or_trigger.strip().lower()
        for candidate_name, s in existing.items():
            if s["trigger_phrase"].strip().lower() == q:
                match_name = candidate_name
                break

    if match_name is None:
        return {"status": "not_found", "message": f"Сценарий «{name_or_trigger}» не найден."}

    scenario = existing[match_name]
    steps = scenario["steps"]
    total = len(steps)

    async def _progress(step_index: int, step_status: str, tool: str, message: str) -> None:
        # Structured event for the web UI's live checklist (see tools/runtime.py);
        # runtime.say() below is the spoken side of the same progress for voice clients.
        await runtime.publish_json(
            runtime.SCENARIO_PROGRESS_TOPIC,
            {
                "scenario": match_name,
                "step_index": step_index,
                "total_steps": total,
                "tool": tool,
                "status": step_status,  # "running" | "done" | "error" | "skipped" | "started" | "finished"
                "message": message,
            },
        )

    await _progress(-1, "started", "", f"Запускаю сценарий «{match_name}», шагов: {total}.")
    await runtime.say(f"Запускаю сценарий «{match_name}», шагов: {total}.")

    for i, step in enumerate(steps, start=1):
        tool = step["tool"]
        args = step.get("args") or {}
        impl = IMPL_REGISTRY.get(tool)
        if impl is None:
            msg = f"Шаг {i} из {total} пропущен: инструмент «{tool}» больше не существует."
            await _progress(i - 1, "skipped", tool, msg)
            await runtime.say(msg)
            continue

        await _progress(i - 1, "running", tool, f"Шаг {i} из {total}: {tool}.")
        await runtime.say(f"Шаг {i} из {total}: {tool}.")
        try:
            result = await impl(**args)
            message = result.get("message", "готово") if isinstance(result, dict) else "готово"
            await _progress(i - 1, "done", tool, message)
        except Exception as exc:
            message = f"ошибка: {exc}"
            await _progress(i - 1, "error", tool, message)
        await runtime.say(message)

    await _progress(total - 1, "finished", "", f"Сценарий «{match_name}» завершён.")
    return {"status": "ok", "message": f"Сценарий «{match_name}» завершён."}


@register_tool
@function_tool
async def run_scenario(context: RunContext, name_or_trigger: str) -> str:
    """Run a saved scenario step by step, announcing progress out loud.

    Args:
        name_or_trigger: The scenario's name or its trigger phrase.
    """
    result = await _run_scenario(name_or_trigger=name_or_trigger)
    return result["message"]
