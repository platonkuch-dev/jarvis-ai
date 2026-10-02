r"""Install or remove the Jarvis voice agent from the current user's Windows startup.

Registers app.py (via the project's own venv pythonw.exe) under
HKCU\Software\Microsoft\Windows\CurrentVersion\Run.

History: a .bat in the Startup folder broke at real logins (cmd.exe reads the
script in the OEM codepage and mangles this project's Cyrillic path). The .lnk
that replaced it avoided that, but Explorer's Startup-folder pass proved
unreliable at boot (2026-10-01: Run keys executed at 08:57, the Startup folder
was never enumerated). A Run value is a Unicode REG_SZ handed straight to
CreateProcess -- no shell text parsing, no Startup-folder delay.

Usage:
    python install_autostart.py            # install
    python install_autostart.py uninstall  # remove
"""

from __future__ import annotations

import sys
import winreg
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STARTUP_DIR = Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
LEGACY_FILES = (STARTUP_DIR / "Jarvis Voice Agent.lnk", STARTUP_DIR / "JarvisVoiceAgent.bat")  # cleaned up if present
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE_NAME = "JarvisVoiceAgent"


def _pythonw() -> Path:
    venv = BASE_DIR / ".venv" / "Scripts" / "pythonw.exe"
    sibling = Path(sys.executable).with_name("pythonw.exe")  # installed layout: runtime\pythonw.exe
    return venv if venv.exists() else sibling if sibling.exists() else Path(sys.executable)


PYTHONW = _pythonw()
APP_PY = BASE_DIR / "app.py"


def _clear_approval() -> None:
    # Windows remembers a Task Manager "disable" per value name; drop any stale verdict.
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        pass


def _remove_legacy() -> bool:
    removed = False
    for path in LEGACY_FILES:
        if path.exists():
            path.unlink()
            removed = True
    return removed


def install() -> None:
    if not PYTHONW.exists():
        print(f"Не найден {PYTHONW}. Сначала создайте venv и установите зависимости (см. README.md).")
        return

    command = f'"{PYTHONW}" "{APP_PY}"'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
    _clear_approval()
    _remove_legacy()

    print(f"Автозапуск установлен: HKCU\\{RUN_KEY}\\{VALUE_NAME}")
    print("Джарвис будет запускаться при входе в Windows (иконка в трее).")


def uninstall() -> None:
    removed = _remove_legacy()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        removed = True
    except FileNotFoundError:
        pass
    _clear_approval()
    print("Автозапуск отключён." if removed else "Автозапуск не был установлен -- нечего удалять.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "uninstall":
        uninstall()
    else:
        install()
