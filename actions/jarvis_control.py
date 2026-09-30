"""User-facing management actions for JARVIS state."""
from __future__ import annotations

import re

from core.assistant_state import get_state, now_iso, record_activity, update_state


def _find(items: list[dict], item_id: str) -> dict | None:
    return next((item for item in items if item.get("id") == item_id), None)


def _render_tasks(state: dict) -> str:
    tasks = state["tasks"]
    if not tasks:
        return "No tasks yet."
    return "\n".join(f"[{task['status'].upper()}] {task['id']} - {task['title']}" for task in tasks)


def _render_routines(state: dict) -> str:
    routines = state["routines"]
    if not routines:
        return "No saved routines."
    return "\n".join(f"{routine['id']}: {routine['name']} - {routine['instruction']}" for routine in routines)


def jarvis_control(parameters: dict | None = None, player=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "status")).strip().lower()
    value = str(params.get("value", "")).strip()
    item_id = str(params.get("id", "")).strip()

    if action == "status":
        state = get_state()
        focus = "active" if state["focus"]["active"] else "off"
        qwen = "enabled" if state["local_llm"]["enabled"] else "off"
        return f"Focus mode: {focus}. Qwen fallback: {qwen}. Tasks: {len(state['tasks'])}. Routines: {len(state['routines'])}."

    if action == "task_list":
        return _render_tasks(get_state())

    if action == "task_add":
        if not value:
            return "Provide a task title."
        task_id = f"task-{int(__import__('time').time() * 1000)}"
        update_state(lambda state: state["tasks"].insert(0, {
            "id": task_id, "title": value, "status": "open", "created_at": now_iso(), "updated_at": now_iso(),
        }))
        record_activity("task_add", value)
        return f"Task added: {task_id} - {value}"

    if action in {"task_complete", "task_cancel"}:
        status = "complete" if action == "task_complete" else "cancelled"
        changed = False
        def update_task(state: dict) -> None:
            nonlocal changed
            task = _find(state["tasks"], item_id)
            if task:
                task["status"], task["updated_at"] = status, now_iso()
                changed = True
        update_state(update_task)
        if changed:
            record_activity(action, item_id)
            return f"Task {item_id} marked {status}."
        return f"Task {item_id} was not found."

    if action == "privacy_get":
        privacy = get_state()["privacy"]
        return "\n".join(f"{name}: {'allowed' if allowed else 'blocked'}" for name, allowed in privacy.items())

    if action == "privacy_set":
        if value not in {"camera", "screen", "browser", "files", "messaging", "remote"}:
            return "Choose one of: camera, screen, browser, files, messaging, remote."
        allowed = str(params.get("enabled", "true")).lower() in {"true", "1", "yes", "on"}
        update_state(lambda state: state["privacy"].__setitem__(value, allowed))
        record_activity("privacy_set", f"{value}={'allowed' if allowed else 'blocked'}")
        return f"Privacy permission for {value}: {'allowed' if allowed else 'blocked'}."

    if action in {"focus_on", "focus_off", "focus_status"}:
        if action == "focus_status":
            focus = get_state()["focus"]
            return f"Focus mode is {'active' if focus['active'] else 'off'}{f' until {focus['until']}' if focus['until'] else ''}."
        enabled = action == "focus_on"
        update_state(lambda state: state["focus"].update({"active": enabled, "started_at": now_iso() if enabled else "", "until": value if enabled else ""}))
        record_activity(action, value or "")
        return f"Focus mode {'enabled' if enabled else 'disabled'}."

    if action == "voice_set":
        if value not in {"brief", "balanced", "detailed"}:
            return "Choose voice style: brief, balanced, or detailed."
        update_state(lambda state: state["voice"].__setitem__("reply_length", value))
        record_activity("voice_set", value)
        return f"Response style set to {value}. It applies on the next Gemini connection."

    if action in {"qwen_on", "qwen_off", "qwen_status"}:
        if action == "qwen_status":
            local = get_state()["local_llm"]
            return f"Qwen offline fallback is {'enabled' if local['enabled'] else 'off'} (model: {local['model']})."
        enabled = action == "qwen_on"
        update_state(lambda state: state["local_llm"].update({"enabled": enabled, "model": "qwen2.5:7b"}))
        record_activity(action, "qwen2.5:7b")
        return "Qwen 2.5 7B offline fallback enabled. Start Ollama and run 'ollama pull qwen2.5:7b' if it is not installed." if enabled else "Qwen offline fallback disabled."

    if action in {"power_on", "power_off", "power_status"}:
        if action == "power_status":
            return f"Power Mode is {'enabled' if get_state()['power_mode']['enabled'] else 'off'}."
        enabled = action == "power_on"
        update_state(lambda state: (
            state["power_mode"].update({"enabled": enabled, "activated_at": now_iso() if enabled else ""}),
            state["local_llm"].__setitem__("enabled", enabled),
            state["voice"].__setitem__("reply_length", "detailed" if enabled else "brief"),
        ))
        record_activity(action, "manual")
        return f"Power Mode {'enabled' if enabled else 'disabled'}."

    if action == "routine_list":
        return _render_routines(get_state())

    if action == "routine_save":
        name = str(params.get("name", "")).strip()
        if not name or not value:
            return "Provide both a routine name and instruction."
        routine_id = f"routine-{int(__import__('time').time() * 1000)}"
        update_state(lambda state: state["routines"].append({"id": routine_id, "name": name, "instruction": value, "created_at": now_iso()}))
        record_activity("routine_save", name)
        return f"Routine saved: {routine_id} - {name}."

    if action == "routine_schedule":
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            return "Provide a daily time in HH:MM 24-hour format."
        changed = False
        def schedule_routine(state: dict) -> None:
            nonlocal changed
            routine = _find(state["routines"], item_id)
            if routine:
                routine["schedule"] = value
                changed = True
        update_state(schedule_routine)
        if changed:
            record_activity("routine_schedule", f"{item_id} at {value}")
            return f"Routine {item_id} will run daily at {value}."
        return f"Routine {item_id} was not found."

    if action == "routine_delete":
        before = len(get_state()["routines"])
        state = update_state(lambda current: current.__setitem__("routines", [item for item in current["routines"] if item.get("id") != item_id]))
        if len(state["routines"]) < before:
            record_activity("routine_delete", item_id)
            return f"Routine {item_id} deleted."
        return f"Routine {item_id} was not found."

    if action == "routine_run":
        routine = _find(get_state()["routines"], item_id)
        if routine:
            record_activity("routine_run", routine["name"])
            return f"[ROUTINE] {routine['instruction']}"
        return f"Routine {item_id} was not found."

    if action == "activity":
        activity = get_state()["activity"][:20]
        return "\n".join(f"{entry['at']} | {entry['action']} | {entry['detail']}" for entry in activity) or "No recorded activity."

    return "Unknown jarvis_control action."