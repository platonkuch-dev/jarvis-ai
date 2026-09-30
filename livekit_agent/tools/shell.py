"""run_powershell: full control of the PC for brains that have no shell of
their own (LLM_PROVIDER=anthropic / openai / ollama). Under claude_code the
brain uses Claude Code's built-in PowerShell instead and worker.py leaves this
tool out; both go through the same pc_guard.py rules.

Live conversation: harmless commands run at once, dangerous ones wait for the
user's spoken "да" (pc_guard.check / note_user_reply), catastrophic ones never
run. Background tasks and scenarios call the impl directly after policy.py
has already asked the owner on Telegram, so the impl itself only refuses the
catastrophic ones.
"""

from __future__ import annotations

import asyncio
import subprocess

from livekit.agents import RunContext, function_tool

import config
import pc_guard
from tools._logging import log_call
from tools.registry import register_impl, register_tool

_MAX_OUTPUT = 4000


@register_impl("run_powershell")
@log_call("run_powershell")
async def _run_powershell(*, command: str, timeout_s: int = 120) -> dict:
    verdict, reason = pc_guard.classify("PowerShell", {"command": command})
    if verdict == pc_guard.BLOCK:
        return {"status": "blocked", "message": pc_guard.block_message(reason)}

    def _run() -> subprocess.CompletedProcess:
        return subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command],
            capture_output=True, timeout=max(5, min(int(timeout_s), 1800)),
            creationflags=config.NO_WINDOW,
        )

    try:
        proc = await asyncio.to_thread(_run)
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": f"Команда не уложилась в {timeout_s} с и была остановлена."}
    except Exception as exc:
        return {"status": "error", "message": f"Не удалось запустить PowerShell: {exc}"}
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    err = proc.stderr.decode("utf-8", errors="replace").strip()
    text = out + (f"\n[ошибки]\n{err}" if err else "")
    if len(text) > _MAX_OUTPUT:
        text = text[:_MAX_OUTPUT] + f"\n[обрезано, всего {len(text)} символов]"
    return {"status": "ok" if proc.returncode == 0 else "error",
            "message": text or ("Готово." if proc.returncode == 0 else f"Код выхода {proc.returncode}.")}


@register_tool
@function_tool
async def run_powershell(context: RunContext, command: str, timeout_s: int = 120) -> str:
    """Run a PowerShell command on the user's Windows PC (full access) and get its output.

    Use it for anything the other tools don't cover: processes, services,
    settings, installing software with winget, files anywhere on disk.
    Dangerous commands (deleting, killing processes, registry, installs,
    shutdown, sending data out) are held back until the user says "да": then
    call again with exactly the same command. Never try to get around a refusal.

    Args:
        command: The PowerShell command line.
        timeout_s: Seconds before the command is stopped (max 1800).
    """
    verdict, reason = pc_guard.check("PowerShell", {"command": command})
    if verdict == pc_guard.CONFIRM:
        return pc_guard.confirm_message(reason)
    if verdict == pc_guard.BLOCK:
        return pc_guard.block_message(reason)
    result = await _run_powershell(command=command, timeout_s=timeout_s)
    return result["message"]
