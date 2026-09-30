"""Coding agent: opens an existing project in VS Code and, optionally, kicks
off a real Claude Code session for it.

Two ways to run that session:
  - config.CODING_AGENT_BACKGROUND (the default, chosen by the owner):
    headless `claude -p` in the project folder, no window, with permissions
    granted up front (bypassPermissions) so it never stops to ask. The
    destructive commands in config.CLAUDE_CODE_DISALLOWED_TOOLS stay blocked
    (deny rules still apply in that mode -- verified). Jarvis says the result
    out loud when it is done.
  - CODING_AGENT_BACKGROUND=0: a new, visible, interactive terminal with
    Claude Code's own approval prompts, exactly as if the user had typed
    `claude` themselves.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from livekit.agents import RunContext, function_tool

import config
from tools._logging import log_call
from tools.registry import register_impl, register_tool

logger = logging.getLogger("jarvis-voice-agent.coding_agent")

# Strong refs: asyncio keeps only weak ones to running tasks.
_BACKGROUND: set[asyncio.Task] = set()


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


async def _run_claude_background(path: Path, task: str) -> None:
    """Headless Claude Code run in `path`; speaks/notifies the outcome."""
    import notify
    from tools import runtime

    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)  # the subscription, not the API key
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    args = [shutil.which("claude"), "-p", task, "--model", config.CODING_AGENT_MODEL,
            "--permission-mode", "bypassPermissions", "--output-format", "json"]
    if config.CLAUDE_CODE_DISALLOWED_TOOLS:
        args += ["--disallowedTools", *config.CLAUDE_CODE_DISALLOWED_TOOLS]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(path), env=env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=config.NO_WINDOW,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=config.CODING_AGENT_TIMEOUT_S)
        try:
            result = json.loads(stdout).get("result") or ""
        except ValueError:
            result = stderr.decode(errors="replace")[-300:]
        spoken = f"Клод Код закончил работу над «{path.name}». {result.strip()[:600]}"
    except asyncio.TimeoutError:
        if proc is not None:
            proc.kill()
        spoken = f"Клод Код слишком долго работал над «{path.name}», я его остановил."
    except Exception as exc:
        logger.warning("background claude failed", exc_info=True)
        spoken = f"Не получилось запустить Клод Код для «{path.name}»: {exc}"
    logger.info("coding agent done: %s", spoken[:300])
    if not runtime.user_present():
        notify.notify_owner(f"💻 {spoken}", kind="task")
    await runtime.say(spoken)


def _run(action: str, project: str, task: str) -> dict:
    path = _resolve_project_path(project)
    if path is None:
        return {
            "status": "not_found",
            "message": f"Не нашёл проект «{project}» ни по пути, ни среди папок в {config.PROJECTS_DIR}.",
        }

    opened_vscode = _open_in_vscode(path)
    if action == "open":
        if not opened_vscode:
            return {"status": "error", "message": "Не нашёл VS Code (команда 'code' не в PATH)."}
        return {"status": "ok", "message": f"Открываю проект «{path.name}» в VS Code."}

    if config.CODING_AGENT_BACKGROUND:
        if not shutil.which("claude"):
            return {"status": "error", "message": "Не нашёл Claude Code (команда 'claude' не в PATH)."}
        return {"status": "background", "path": path}

    if not opened_vscode:
        return {"status": "error", "message": "Не нашёл VS Code (команда 'code' не в PATH)."}
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
    result = await asyncio.to_thread(_run, action, project, task)
    if result.get("status") == "background":
        path = result["path"]
        job = asyncio.create_task(_run_claude_background(path, task), name="coding-agent")
        _BACKGROUND.add(job)
        job.add_done_callback(_BACKGROUND.discard)
        return {"status": "ok", "message": f"Запустил Клод Код в фоне для «{path.name}» с задачей: {task}. "
                                           "Без окон и вопросов — скажу, когда закончит."}
    return result


@register_tool
@function_tool
async def coding_agent(context: RunContext, action: CodingAgentAction, project: str, task: str = "") -> str:
    """Open an existing coding project in VS Code, and optionally start a
    separate Claude Code session in it so it can actually write/edit code.
    Only for programming work in a code project -- documents, spreadsheets
    and presentations you make yourself, not through this tool. By default
    that session runs in the background with no window and reports back by
    voice when it is done.

    Args:
        action: "open" just opens the project in VS Code. "start" also runs
            Claude Code in that project with `task` as its job.
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
