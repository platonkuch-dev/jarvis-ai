"""Risk levels for tool calls made without a person in the loop.

When the user is talking to Jarvis, the user is the safety check: they asked
for it and hear what happens. A background task or a trigger has nobody
watching, so every call it makes goes through `classify()` first:

  "safe"    -- read-only or trivially reversible; runs immediately.
  "confirm" -- has a real side effect (sends something, closes/changes
               something, drives the screen); needs the owner's "да" on
               Telegram unless the task was explicitly trusted.
  "block"   -- never done autonomously (recursion into task/trigger
               management, sleep the PC out from under the agent).

This lives in code on purpose: a prompt can be argued with, a lookup table
can't. Unknown tools default to "confirm".
"""

from __future__ import annotations

SAFE = "safe"
CONFIRM = "confirm"
BLOCK = "block"

_SAFE_TOOLS = {
    "calculate", "get_weather", "web_search", "get_system_status", "take_screenshot",
    "find_suspicious_processes", "read_last_log", "read_clipboard", "search_notes", "write_note",
    "draft_email", "list_memory", "remember_fact", "get_schedule", "create_event", "add_todo",
    "complete_todo", "create_reminder", "set_timer", "list_scenarios", "open_application",
    "media_control", "find_and_open_file", "change_voice", "look_at_camera",
    "watch_screen", "stop_watching_screen", "stop_computer_use", "list_tasks", "task_status",
    "list_triggers",
}
_CONFIRM_TOOLS = {
    "send_telegram_message", "remember_person_fact", "close_application", "use_computer",
    "coding_agent", "run_predefined_script", "run_scenario", "forget_fact",
    "after_effects_control", "photoshop_control", "premiere_control",
}
_BLOCK_TOOLS = {
    "start_task", "cancel_task", "create_trigger", "delete_trigger", "toggle_trigger",
    "create_scenario", "edit_scenario", "delete_scenario",
}

# Per-action refinements for multi-action tools.
_ACTION_LEVELS: dict[str, dict[str, str]] = {
    "system_control": {"volume": SAFE, "brightness": SAFE, "lock": CONFIRM,
                       "wifi": CONFIRM, "bluetooth": CONFIRM, "sleep": BLOCK},
    "window_manager": {"list_windows": SAFE, "list_processes": SAFE, "get_active": SAFE,
                       "focus": SAFE, "minimize": SAFE, "maximize": SAFE, "restore": SAFE,
                       "move": SAFE, "resize": SAFE, "wait_for_window": SAFE,
                       "launch_or_focus": SAFE, "close": CONFIRM},
    "desktop_control": {"list": SAFE, "stats": SAFE, "current_wallpaper": SAFE},
    "file_manager": {"list": SAFE, "info": SAFE, "find": SAFE, "read": SAFE, "largest": SAFE,
                     "disk_usage": SAFE, "create_folder": SAFE},
    "quick_ui": {"read": SAFE},
    "ui_automation": {"find": SAFE, "read": SAFE, "get_tree": SAFE, "wait_for_element": SAFE},
}


def classify(tool: str, args: dict | None = None) -> str:
    args = args or {}
    if tool in _BLOCK_TOOLS:
        return BLOCK
    if tool in _ACTION_LEVELS:
        return _ACTION_LEVELS[tool].get(str(args.get("action", "")), CONFIRM)
    if tool in _SAFE_TOOLS:
        return SAFE
    return CONFIRM


def describe(tool: str, args: dict | None = None) -> str:
    """Human-readable one-liner for an approval question."""
    args = args or {}
    shown = ", ".join(f"{k}={str(v)[:80]}" for k, v in args.items() if v not in (None, "", False))
    return f"{tool}({shown})" if shown else tool
