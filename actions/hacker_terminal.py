"""
Hacker Terminal Mode — arbitrary PowerShell/CMD execution triggered by voice.

The user explicitly chose this over a curated command whitelist (asked
directly, declined the safer option). The one safety mechanism they did
agree to is a confirmation gate: JARVIS must describe the exact command out
loud and get an explicit yes before it runs. The project's own shared
confirmation gate (core/tool_registry.py's GATED_LEVELS) is a deliberate
no-op everywhere else in this codebase -- nothing else is gated today, not
even restart/shutdown -- so this tool is force-gated locally in
core/tool_dispatch.py instead of relying on that shared switch, the same
way actions/computer_settings.py already has its own hardcoded redundant
check for restart/shutdown. See core/tool_dispatch.py's
_LOCALLY_GATED_TOOLS for the enforcement point; nothing in this file trusts
the caller to have confirmed anything.

Every command run through here — confirmed and executed, not just
attempted — is appended to logs/hacker_terminal.log with full,
untruncated output, independent of the generic 300-char/100-entry activity
feed in core/assistant_state.py. That feed still gets its own generic
entry from tool_dispatch.py; this is the durable, complete audit trail
specifically for this tool.
"""
from __future__ import annotations

import subprocess
import time
from datetime import datetime
from pathlib import Path

_LOG_PATH = Path(__file__).parent.parent / "logs" / "hacker_terminal.log"

_DEFAULT_TIMEOUT = 30.0
_MAX_TIMEOUT     = 120.0

_SPOKEN_PREVIEW_CHARS = 400
_PANEL_PREVIEW_CHARS  = 6000


def _run_powershell(command: str, cwd: str, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, timeout=timeout, cwd=cwd,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _run_cmd(command: str, cwd: str, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["cmd", "/c", command],
        capture_output=True, text=True, timeout=timeout, cwd=cwd,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _append_log(shell: str, command: str, cwd: str, exit_code: int | None,
                 stdout: str, stderr: str, elapsed_s: float, timed_out: bool) -> None:
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n{'=' * 70}\n")
        f.write(f"[{datetime.now().isoformat(timespec='seconds')}] shell={shell} cwd={cwd} "
                f"elapsed={elapsed_s:.2f}s timed_out={timed_out} exit_code={exit_code}\n")
        f.write(f"$ {command}\n")
        if stdout:
            f.write(f"--- stdout ---\n{stdout}\n")
        if stderr:
            f.write(f"--- stderr ---\n{stderr}\n")


def run_terminal_command(parameters: dict | None = None, player=None) -> str:
    parameters = parameters or {}
    command = str(parameters.get("command", "")).strip()
    if not command:
        return "No command given — nothing executed."

    shell = str(parameters.get("shell", "powershell")).strip().lower()
    if shell not in ("powershell", "cmd"):
        shell = "powershell"

    cwd = str(parameters.get("working_dir") or Path.home())
    if not Path(cwd).is_dir():
        return f"Working directory does not exist: {cwd} — nothing executed."

    try:
        timeout = float(parameters.get("timeout", _DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT
    timeout = max(1.0, min(timeout, _MAX_TIMEOUT))

    runner = _run_powershell if shell == "powershell" else _run_cmd

    t0 = time.monotonic()
    timed_out = False
    try:
        proc = runner(command, cwd, timeout)
        stdout, stderr, exit_code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as e:
        timed_out = True
        stdout = (e.stdout or "") if isinstance(e.stdout, str) else ""
        stderr = (e.stderr or "") if isinstance(e.stderr, str) else ""
        exit_code = None
    except Exception as e:
        elapsed = time.monotonic() - t0
        _append_log(shell, command, cwd, None, "", str(e), elapsed, False)
        return f"Failed to execute: {e}"
    elapsed = time.monotonic() - t0

    _append_log(shell, command, cwd, exit_code, stdout, stderr, elapsed, timed_out)

    combined = (stdout or "") + (("\n" + stderr) if stderr else "")
    combined = combined.strip()

    if player is not None and hasattr(player, "show_content"):
        header = f"$ {command}\n(shell={shell}  cwd={cwd}  exit_code={exit_code}  {elapsed:.1f}s)\n\n"
        body = header + (combined[:_PANEL_PREVIEW_CHARS] or "(no output)")
        if len(combined) > _PANEL_PREVIEW_CHARS:
            body += f"\n... [{len(combined) - _PANEL_PREVIEW_CHARS} more chars truncated, full output in logs/hacker_terminal.log]"
        player.show_content("HACKER TERMINAL", body)

    if timed_out:
        return f"Command timed out after {timeout:.0f}s and was killed: {command}"

    preview = combined[:_SPOKEN_PREVIEW_CHARS]
    if not preview:
        return f"Command finished (exit code {exit_code}), no output."
    suffix = "..." if len(combined) > _SPOKEN_PREVIEW_CHARS else ""
    return f"Command finished (exit code {exit_code}). Output: {preview}{suffix}"
