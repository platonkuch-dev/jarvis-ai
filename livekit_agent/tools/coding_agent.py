"""Coding agent: opens an existing project in VS Code and, optionally, kicks
off a real Claude Code session for it in a new, visible terminal window.

Deliberately does NOT pass --allow-dangerously-skip-permissions or any other
permission-bypassing flag to `claude` -- the whole point of a visible,
interactive terminal is that Claude Code's own tool-approval prompts still
work exactly as if the user had typed `claude` themselves. Jarvis starts the
session; it never gets to skip its guardrails, matching this project's own
"never bypass safety checks" rule for every other tool here.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from livekit.agents import RunContext, function_tool

import config
from tools._logging import log_call
from tools.registry import register_impl, register_tool


# How the user/LLM refers to "Джарвис's own code" -- config.BASE_DIR (this
# very livekit_agent/ folder) is a *subfolder* of a project search root, not
# a top-level entry itself, so the normal by-name folder search below would
# never find it. Special-cased instead of indexed, since it's not really a
# "project among many" as much as a fixed, known answer.
_SELF_ALIASES = {
    "себя", "себя самого", "свой код", "свой проект", "джарвис",
    "джарвиса", "этот проект", "самого себя", "self", "this project",
}


def _resolve_project_path(name_or_path: str) -> Path | None:
    key = name_or_path.strip().lower()
    if key in _SELF_ALIASES:
        return config.BASE_DIR

    candidate = Path(name_or_path).expanduser()
    if candidate.is_dir():
        return candidate

    query = key.replace(" ", "")
    if not query:
        return None

    exact: list[Path] = []
    partial: list[Path] = []
    for root in config.PROJECT_SEARCH_DIRS:
        try:
            for entry in root.iterdir():
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                stem = entry.name.lower().replace(" ", "")
                if stem == query:
                    exact.append(entry)
                elif query in stem or stem in query:
                    partial.append(entry)
        except Exception:
            continue

    matches = exact + partial
    return matches[0] if matches else None


def _open_in_vscode(path: Path) -> bool:
    code_cmd = shutil.which("code") or shutil.which("code.cmd")
    if not code_cmd:
        return False
    try:
        subprocess.Popen([code_cmd, str(path)])
        return True
    except Exception:
        return False


def _launch_claude_terminal(path: Path, task: str) -> bool:
    """Opens a new terminal window in `path` running `claude` with `task` as
    its opening prompt -- interactive and visible, no bypassed permissions."""
    if not shutil.which("claude"):
        return False
    claude_args = ["claude", "--model", config.CODING_AGENT_MODEL, task]
    try:
        if config.SYSTEM == "Windows":
            if shutil.which("wt"):
                subprocess.Popen(["wt", "-d", str(path), "cmd", "/k", *claude_args])
            else:
                subprocess.Popen(
                    ["cmd", "/k", *claude_args],
                    cwd=str(path),
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                )
        elif config.SYSTEM == "Darwin":
            claude_cmd = f"claude --model {config.CODING_AGENT_MODEL} {task!r}"
            subprocess.Popen(
                ["osascript", "-e", f'tell app "Terminal" to do script "cd {path} && {claude_cmd}"']
            )
        else:
            terminal = shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal") or "xterm"
            claude_cmd = f"claude --model {config.CODING_AGENT_MODEL} {task!r}"
            subprocess.Popen([terminal, "-e", claude_cmd], cwd=str(path))
        return True
    except Exception:
        return False


def _run(action: str, project: str, task: str) -> dict:
    path = _resolve_project_path(project)
    if path is None:
        return {
            "status": "not_found",
            "message": f"Не нашёл проект «{project}» ни по пути, ни среди папок в {config.PROJECTS_DIR}.",
        }

    opened_vscode = _open_in_vscode(path)
    if not opened_vscode:
        return {"status": "error", "message": "Не нашёл VS Code (команда 'code' не в PATH)."}

    if action == "open":
        return {"status": "ok", "message": f"Открываю проект «{path.name}» в VS Code."}

    launched = _launch_claude_terminal(path, task)
    if not launched:
        return {
            "status": "error",
            "message": (
                f"Открыл «{path.name}» в VS Code, но не нашёл Claude Code (команда "
                "'claude' не в PATH) -- запустите его вручную в терминале."
            ),
        }
    return {
        "status": "ok",
        "message": f"Открываю проект «{path.name}» в VS Code и запускаю Claude Code с задачей: {task}",
    }


CodingAgentAction = Literal["open", "start"]


@register_impl("coding_agent")
@log_call("coding_agent")
async def _coding_agent(*, action: str, project: str, task: str = "") -> dict:
    if not project:
        return {"status": "error", "message": "Не указано название или путь проекта."}
    if action == "start" and not task:
        return {"status": "error", "message": "Для запуска Claude Code нужна задача (что делать в проекте)."}
    return await asyncio.to_thread(_run, action, project, task)


@register_tool
@function_tool
async def coding_agent(context: RunContext, action: CodingAgentAction, project: str, task: str = "") -> str:
    """Open an existing coding project in VS Code, and optionally start a
    real Claude Code session for it in a new terminal window so it can
    actually write/edit code -- the same Claude Code you (Джарвис's LLM
    brain) are built on, running as its own separate, supervised session
    with its own normal permission prompts (never bypassed).

    Args:
        action: "open" just opens the project in VS Code. "start" also opens
            a new terminal in that project and launches `claude` there
            (pinned to config.CODING_AGENT_MODEL, Sonnet 5) with `task` as
            the opening prompt -- the user sees and can guide/approve
            everything Claude Code does, exactly like using it themselves.
        project: The project's folder name (searched under the user's
            projects directory and Desktop), a full path, or "себя" to mean
            Джарвис's own source code (this very project) when the user wants
            it to work on itself.
        task: What to ask Claude Code to do, in the same language the user
            asked in (e.g. "add a dark mode toggle to the settings page").
            Required for action="start".
    """
    result = await _coding_agent(action=action, project=project, task=task)
    return result["message"]
