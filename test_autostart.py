"""Small unit checks for the Windows autostart command builder."""
from pathlib import Path

from core.autostart import _build_task_xml, build_launch_command


def check(label: str, actual: str, expected: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")
    print(f"[PASS] {label}")


def check_contains(label: str, haystack: str, needle: str) -> None:
    if needle not in haystack:
        raise AssertionError(f"{label}: expected to find {needle!r} in generated XML")
    print(f"[PASS] {label}")


script = Path(r"C:\Program Files\MARK XLVIII\main.py")
check(
    "Python script command is fully quoted",
    build_launch_command(script, executable=r"C:\Python\python.exe", frozen=False),
    '"C:\\Python\\python.exe" "C:\\Program Files\\MARK XLVIII\\main.py"',
)
check(
    "Frozen executable has no script argument",
    build_launch_command(script, executable=r"C:\Program Files\MARK XLVIII\jarvis.exe", frozen=True),
    '"C:\\Program Files\\MARK XLVIII\\jarvis.exe"',
)

xml = _build_task_xml(script)
check_contains("Task starts on user logon", xml, "<LogonTrigger>")
check_contains("Task runs with the highest available privileges", xml, "<RunLevel>HighestAvailable</RunLevel>")
check_contains("Task restarts itself after a crash", xml, "<RestartOnFailure>")
check_contains("Task stays in the interactive user session", xml, "<LogonType>InteractiveToken</LogonType>")