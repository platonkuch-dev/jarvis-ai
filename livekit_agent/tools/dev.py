"""Run a script -- but only ever one named in config.SCRIPT_WHITELIST.

There is no code path here that accepts a path, interpreter, or extra
arguments from the LLM: `script_name` is looked up, and everything actually
executed comes from `config.SCRIPT_WHITELIST[script_name]`.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from livekit.agents import RunContext, function_tool

import config
from tools._logging import log_call
from tools.registry import register_impl, register_tool


def _log_path(script_name: str) -> Path:
    return config.SCRIPTS_LOG_DIR / f"{script_name}.log"


@register_impl("run_predefined_script")
@log_call("run_predefined_script")
async def _run_predefined_script(*, script_name: str) -> dict:
    entry = config.SCRIPT_WHITELIST.get(script_name)
    if entry is None:
        allowed = ", ".join(config.SCRIPT_WHITELIST) or "(список пуст)"
        return {
            "status": "error",
            "message": f"Скрипт «{script_name}» не в белом списке. Доступные: {allowed}.",
        }

    executable = str(entry["executable"])
    args = [str(a) for a in entry.get("args", [])]
    timeout = float(entry.get("timeout", 60))
    log_path = _log_path(script_name)

    try:
        proc = await asyncio.create_subprocess_exec(
            executable,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return {"status": "error", "message": f"Скрипт «{script_name}» превысил лимит времени."}
    except Exception as exc:
        return {"status": "error", "message": f"Не удалось запустить «{script_name}»: {exc}"}

    output = stdout.decode(errors="replace") if stdout else ""

    def _write_log() -> None:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} (exit {proc.returncode}) ===\n")
            f.write(output)

    await asyncio.to_thread(_write_log)

    if proc.returncode == 0:
        return {"status": "ok", "message": f"Скрипт «{script_name}» выполнен успешно."}
    return {
        "status": "error",
        "message": f"Скрипт «{script_name}» завершился с ошибкой (код {proc.returncode}).",
    }


@register_tool
@function_tool
async def run_predefined_script(context: RunContext, script_name: str) -> str:
    """Run a pre-approved script from the whitelist in config.py. No arbitrary shell/code execution.

    Args:
        script_name: The whitelist key of the script to run.
    """
    result = await _run_predefined_script(script_name=script_name)
    return result["message"]


@register_impl("read_last_log")
@log_call("read_last_log")
async def _read_last_log(*, script_name: str, lines: int = 20) -> dict:
    log_path = _log_path(script_name)
    if not log_path.exists():
        return {"status": "not_found", "message": f"Логов для «{script_name}» пока нет."}

    def _tail() -> str:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            content = f.readlines()
        return "".join(content[-lines:])

    tail = await asyncio.to_thread(_tail)
    return {"status": "ok", "message": tail or "(лог пуст)"}


@register_tool
@function_tool
async def read_last_log(context: RunContext, script_name: str, lines: int = 20) -> str:
    """Read the tail of a predefined script's last run log.

    Args:
        script_name: The whitelist key of the script whose log to read.
        lines: How many trailing lines to return, defaults to 20.
    """
    result = await _read_last_log(script_name=script_name, lines=lines)
    return result["message"]
