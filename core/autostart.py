"""Windows startup registration for MARK XLVIII.

Registers a per-user Task Scheduler task (logon trigger, highest available
run level, auto-restart on failure) instead of a plain HKCU\\Run entry. This
keeps JARVIS in the interactive user session -- so the HUD, microphone, and
windows_control keep working -- while still starting elevated, hidden, and
resilient to crashes without a manual launch.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree
from xml.sax.saxutils import escape

APP_NAME = "MARK XLVIII"
TASK_NAME = APP_NAME

_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Starts MARK XLVIII when {user} signs in.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowHardTerminate>true</AllowHardTerminate>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _hidden_executable(executable: str) -> str:
    """Swap console python.exe for the windowless pythonw.exe when present."""
    candidate = Path(executable).with_name("pythonw.exe")
    return str(candidate) if candidate.is_file() else executable


def _default_executable(script_path: Path) -> str:
    """Resolve the interpreter to launch at logon when none is given
    explicitly. Prefers the project's own .venv (README's Quick Start has
    the user run `.venv\\Scripts\\python.exe main.py`, and that's where
    `pip install -r requirements.txt` actually landed packages like
    openwakeword/sentence-transformers) over whatever `sys.executable`
    happens to be -- if --install-autostart is run from a different
    interpreter (e.g. a global/Store Python with none of those packages),
    using sys.executable would silently register a task that can never
    detect the wake word. Falls back to sys.executable if no .venv is
    found next to script_path, preserving prior behavior."""
    venv_pythonw = script_path.resolve().parent / ".venv" / "Scripts" / "pythonw.exe"
    if venv_pythonw.is_file():
        return str(venv_pythonw)
    venv_python = script_path.resolve().parent / ".venv" / "Scripts" / "python.exe"
    if venv_python.is_file():
        return str(venv_python)
    return _hidden_executable(sys.executable)


def build_launch_command(script_path: Path, executable: str | None = None, frozen: bool | None = None) -> str:
    """Build the quoted command Windows runs after the user signs in."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if executable is None:
        executable = sys.executable if frozen else _default_executable(script_path)
    if frozen:
        return f'"{executable}"'
    return f'"{executable}" "{script_path.resolve()}"'


def _current_user() -> str:
    domain = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME", "")
    name = os.environ.get("USERNAME", "")
    return f"{domain}\\{name}" if domain else name


def _split_launch_command(script_path: Path) -> tuple[str, str, str]:
    """Return (executable, arguments, working_directory) for the task XML."""
    frozen = getattr(sys, "frozen", False)
    executable = sys.executable if frozen else _default_executable(script_path)
    arguments = "" if frozen else f'"{script_path.resolve()}"'
    workdir = str((script_path.resolve().parent if not frozen else Path(executable).parent))
    return executable, arguments, workdir


def _build_task_xml(script_path: Path) -> str:
    user = _current_user()
    executable, arguments, workdir = _split_launch_command(script_path)
    return _TASK_XML.format(
        user=escape(user),
        command=escape(executable),
        arguments=escape(arguments),
        workdir=escape(workdir),
    )


def _run_schtasks(args: list[str]) -> subprocess.CompletedProcess:
    """Run schtasks.exe, capturing raw bytes.

    schtasks writes status text in the console's active codepage (which does
    not necessarily match Python's locale-preferred encoding, e.g. an OEM
    codepage vs. cp1251) and writes /XML output as UTF-16. Decoding either as
    text up front risks a UnicodeDecodeError, so callers decode explicitly.
    """
    if os.name != "nt":
        raise OSError("Autostart is currently supported only on Windows.")
    return subprocess.run(
        ["schtasks", *args],
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _decode_console(data: bytes) -> str:
    return data.decode("oem", errors="replace")


def install(script_path: Path) -> str:
    if os.name != "nt":
        raise OSError("Autostart is currently supported only on Windows.")
    xml = _build_task_xml(script_path)
    with tempfile.NamedTemporaryFile("w", suffix=".xml", encoding="utf-16", delete=False) as handle:
        handle.write(xml)
        xml_path = handle.name
    try:
        result = _run_schtasks(["/Create", "/TN", TASK_NAME, "/XML", xml_path, "/F"])
        if result.returncode != 0:
            message = _decode_console(result.stderr or result.stdout).strip()
            raise OSError(f"schtasks failed: {message}")
    finally:
        Path(xml_path).unlink(missing_ok=True)
    executable, arguments, _ = _split_launch_command(script_path)
    return f'"{executable}" {arguments}'.strip()


def remove() -> bool:
    result = _run_schtasks(["/Delete", "/TN", TASK_NAME, "/F"])
    return result.returncode == 0


_TASK_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def status() -> str | None:
    result = _run_schtasks(["/Query", "/TN", TASK_NAME, "/XML"])
    if result.returncode != 0:
        return None
    # schtasks declares encoding="UTF-16" in the XML prolog but, when its
    # stdout is redirected (as opposed to a real console), actually writes
    # single-byte OEM-codepage text. Decode with the same codec used
    # elsewhere and hand ElementTree a str so it doesn't try to reinterpret
    # the (misleading) declared encoding against raw bytes.
    xml_text = _decode_console(result.stdout)
    try:
        root = ElementTree.fromstring(xml_text)
        command = root.find(".//t:Actions/t:Exec/t:Command", _TASK_NS)
        arguments = root.find(".//t:Actions/t:Exec/t:Arguments", _TASK_NS)
    except ElementTree.ParseError:
        return TASK_NAME
    if command is None:
        return TASK_NAME
    text = command.text or ""
    if arguments is not None and arguments.text:
        text = f"{text} {arguments.text}"
    return text.strip()
