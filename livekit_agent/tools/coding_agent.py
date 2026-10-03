"""Coding agent: all of Jarvis's programming goes through here, and it always
happens on the hologram.

"start" runs Claude Code headless (`claude -p`) in the project folder -- no
console, no VS Code window -- with permissions granted up front
(bypassPermissions) so it never stops to ask; the destructive commands in
config.CLAUDE_CODE_DISALLOWED_TOOLS stay blocked (deny rules still apply in
that mode -- verified). Its stream-json output is written step by step to
code_feed.py, which the holographic panel shows live (reads, edits,
commands, Claude's words) with a stop button. When the hologram can't be
seen (it lives on the wallpaper, under the windows), the panel is opened on
config.HUD_PANEL_MONITOR so the work is always in view
(config.CODING_SHOW_HUD). Jarvis says the result out loud when it is done.

The owner chose this over the old visible terminal: there is no console
mode any more. "open" only opens the project in VS Code. A project that
doesn't exist yet gets a new folder under config.PROJECTS_DIR, so "напиши
мне скрипт ..." never ends with the brain writing code itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
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
        return config.JARVIS_SOURCE_DIR or config.BASE_DIR

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


async def _pump_stream(proc: asyncio.subprocess.Process) -> tuple[str, bool]:
    """Feeds Claude Code's stream-json events to the panel; returns (result, ok)."""
    import code_feed

    result, ok = "", False
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "text":
                    code_feed.add("text", block.get("text", ""))
                elif block.get("type") == "tool_use":
                    code_feed.add("tool", code_feed.describe_tool(block.get("name", ""), block.get("input") or {}))
        elif kind == "result":
            result = event.get("result") or ""
            ok = not event.get("is_error") and event.get("subtype", "success") == "success"
    return result, ok


async def _watch_stop(proc: asyncio.subprocess.Process) -> bool:
    """Kills the run when the panel's stop button (or a spoken stop) asks to."""
    import code_feed

    while proc.returncode is None:
        if code_feed.stop_requested():
            proc.kill()
            return True
        await asyncio.sleep(0.5)
    return False


_SELF_RELEASE_NOTE = ("\n\n(Это исходный код самого Джарвиса. Не коммить, не пушь, не меняй installer/VERSION "
                      "и не собирай установщик — после твоей работы Джарвис сам соберёт, выложит и установит "
                      "новую версию. Если добавляешь зависимость — впиши её и в installer/requirements.lock.)")


def _is_self(path: Path) -> bool:
    src = config.JARVIS_SOURCE_DIR
    try:
        return src is not None and path.resolve() == Path(src).resolve()
    except OSError:
        return False


def _start_self_release(task: str, summary: str) -> bool:
    """Hands the finished change to scripts/self_release.py in its own detached
    process: the install it ends with closes this Jarvis."""
    import notify

    src = Path(config.JARVIS_SOURCE_DIR)
    python = src / ".venv" / "Scripts" / "python.exe"
    script = src / "scripts" / "self_release.py"
    if not python.is_file() or not script.is_file():
        logger.warning("self release unavailable: %s / %s missing", python, script)
        return False
    # Not under the install folder: the installer kills every process whose
    # command line mentions it, which would include this one.
    job = src / "logs" / "self_release_job.json"
    job.parent.mkdir(exist_ok=True)
    job.write_text(json.dumps({"task": task, "summary": summary, "outbox": str(notify.OUTBOX_FILE),
                               "installed_env": str(config.BASE_DIR / ".env")}, ensure_ascii=False),
                   encoding="utf-8")
    detached = 0x00000008 | 0x00000200 | config.NO_WINDOW  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    for flags in (detached | 0x01000000, detached):        # + BREAKAWAY_FROM_JOB when the job allows it
        try:
            subprocess.Popen([str(python), str(script), "--job", str(job)], cwd=str(src), creationflags=flags,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             close_fds=True)
            return True
        except OSError:
            continue
    logger.warning("could not start self release", exc_info=True)
    return False


async def _run_claude_background(path: Path, task: str) -> None:
    """Headless Claude Code run in `path`, live on the panel; speaks/notifies the outcome."""
    import code_feed
    import notify
    from tools import runtime

    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)  # the subscription, not the API key
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    self_release = config.SELF_RELEASE and _is_self(path)
    prompt = task + (_SELF_RELEASE_NOTE if self_release else "")
    args = [shutil.which("claude"), "-p", prompt, "--model", config.CODING_AGENT_MODEL,
            "--permission-mode", "bypassPermissions", "--output-format", "stream-json", "--verbose"]
    if config.CLAUDE_CODE_DISALLOWED_TOOLS:
        args += ["--disallowedTools", *config.CLAUDE_CODE_DISALLOWED_TOOLS]
    code_feed.start(path.name, task)
    proc = None
    ok = False
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(path), env=env, limit=64 * 1024 * 1024,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=config.NO_WINDOW,
        )
        stopper = asyncio.create_task(_watch_stop(proc))
        result, ok = await asyncio.wait_for(_pump_stream(proc), timeout=config.CODING_AGENT_TIMEOUT_S)
        await proc.wait()
        stopped = await stopper
        if stopped:
            ok, result = False, "Остановлено по вашей команде."
            spoken = f"Остановил Клод Код в «{path.name}»."
        else:
            if not result and proc.stderr is not None:
                result = (await proc.stderr.read()).decode(errors="replace")[-300:]
            spoken = f"Клод Код закончил работу над «{path.name}». {result.strip()[:600]}"
    except asyncio.TimeoutError:
        if proc is not None:
            proc.kill()
        result = "Слишком долго — остановлено."
        spoken = f"Клод Код слишком долго работал над «{path.name}», я его остановил."
    except Exception as exc:
        logger.warning("background claude failed", exc_info=True)
        result = str(exc)
        spoken = f"Не получилось запустить Клод Код для «{path.name}»: {exc}"
    code_feed.finish(result, ok)
    if self_release and ok:
        if await asyncio.to_thread(_start_self_release, task, result):
            spoken += (" Теперь собираю новую версию, выкладываю её на ГитХаб и ставлю — "
                       "минут через десять я перезапущусь, ход напишу в Телеграм.")
        else:
            spoken += " Автосборку запустить не смог — нет окружения сборки в папке исходников."
    logger.info("coding agent done: %s", spoken[:300])
    if not runtime.user_present():
        notify.notify_owner(f"💻 {spoken}", kind="task")
    await runtime.say(spoken)


_NAME_RE = re.compile(r"[^\w\- .]+", re.UNICODE)


def _new_project_dir(name: str) -> Path | None:
    """A fresh folder under PROJECTS_DIR for a project that doesn't exist yet."""
    clean = _NAME_RE.sub("", name).strip(" .")
    if not clean or any(sep in name for sep in ("/", "\\", ":")):
        return None
    path = config.PROJECTS_DIR / clean
    path.mkdir(parents=True, exist_ok=True)
    return path


def _show_hologram() -> None:
    """Puts the hologram in view for the coding session (CODING_SHOW_HUD)."""
    if not config.CODING_SHOW_HUD:
        return
    try:
        import hud_panel

        hud_panel.ensure_server()
        if not hud_panel.is_open():
            hud_panel.open_panel(config.HUD_PANEL_MONITOR)
    except Exception:
        logger.warning("could not open the hologram for coding", exc_info=True)


def _run(action: str, project: str, task: str) -> dict:
    path = _resolve_project_path(project)
    created = False
    if path is None and action == "start":
        path = _new_project_dir(project)
        created = path is not None
    if path is None:
        return {
            "status": "not_found",
            "message": f"Не нашёл проект «{project}» ни по пути, ни среди папок в {config.PROJECTS_DIR}.",
        }

    if action == "open":
        if not _open_in_vscode(path):
            return {"status": "error", "message": "Не нашёл VS Code (команда 'code' не в PATH)."}
        return {"status": "ok", "message": f"Открываю проект «{path.name}» в VS Code."}

    if not shutil.which("claude"):
        return {"status": "error", "message": "Не нашёл Claude Code (команда 'claude' не в PATH)."}
    _show_hologram()
    return {"status": "background", "path": path, "created": created}


CodingAgentAction = Literal["open", "start", "stop"]


@register_impl("coding_agent")
@log_call("coding_agent")
async def _coding_agent(*, action: str, project: str = "", task: str = "") -> dict:
    if action == "stop":
        import code_feed

        if code_feed.is_running() and code_feed.request_stop():
            return {"status": "ok", "message": "Останавливаю Клод Код."}
        return {"status": "ok", "message": "Клод Код сейчас ничего не делает."}
    if not project:
        return {"status": "error", "message": "Не указано название или путь проекта."}
    if action == "start" and not task:
        return {"status": "error", "message": "Для запуска Claude Code нужна задача (что делать в проекте)."}
    if action == "start":
        import code_feed

        busy = code_feed.read()
        if code_feed.is_running():
            return {"status": "busy", "message": f"Клод Код ещё работает над «{busy.get('project')}». "
                                                 "Дождись или скажи «стоп», потом дам новую задачу."}
    result = await asyncio.to_thread(_run, action, project, task)
    if result.get("status") == "background":
        path = result["path"]
        job = asyncio.create_task(_run_claude_background(path, task), name="coding-agent")
        _BACKGROUND.add(job)
        job.add_done_callback(_BACKGROUND.discard)
        where = "Создал новую папку проекта и запустил" if result.get("created") else "Запустил"
        return {"status": "ok", "message": f"{where} Клод Код для «{path.name}» с задачей: {task}. "
                                           "Ход работы — на голограмме, скажу, когда закончит."}
    return result


@register_tool
@function_tool
async def coding_agent(context: RunContext, action: CodingAgentAction, project: str = "", task: str = "") -> str:
    """ALL programming goes through this tool: writing, fixing or changing
    code, scripts, bots, sites, programs -- in an existing project or a new
    one. Never write code yourself and never run `claude` in a console.
    Only for programming work in a code project -- documents, spreadsheets
    and presentations you make yourself, not through this tool. This is THE
    way to write code -- never open a terminal/console with `claude`
    yourself. The session runs with no window; its progress is shown live on
    Jarvis's holographic panel, and it reports back by voice when done.

    Args:
        action: "open" just opens the project in VS Code. "start" also runs
            Claude Code in that project with `task` as its job. "stop" stops
            the running Claude Code session (no project needed).
        project: The project's folder name (searched under the user's
            projects directory and Desktop), a full path, or "себя" to mean
            Джарвис's own source code (this very project) when the user wants
            it to work on itself. For something new ("напиши скрипт...") give
            a short new folder name -- "start" creates it.
        task: What to ask Claude Code to do, in the same language the user
            asked in (e.g. "add a dark mode toggle to the settings page").
            Required for action="start".
    """
    result = await _coding_agent(action=action, project=project, task=task)
    return result["message"]
