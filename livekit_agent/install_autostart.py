"""Install or remove the Jarvis voice agent from the current user's Windows startup.

Drops a native .lnk shortcut into the Startup folder (no registry edits) that
launches app.py via the project's own venv pythonw.exe.

A .lnk was chosen over a .bat deliberately: a shortcut's target path is
stored as Unicode COM metadata, never parsed as text by any shell. A .bat
file containing this project's own Cyrillic path as literal text (the
previous approach here) gets silently mangled by cmd.exe reading the script
under the system's default OEM codepage at real Windows startup -- verified
live: it worked every time *I* tested it (through shells that already
tolerate UTF-8), but failed for the user at an actual login with a "cannot
find <mangled path>" error, since a genuine boot-time cmd.exe uses the
OS locale's OEM codepage instead. Windows Explorer's own Desktop shortcuts
(see scripts/create_shortcut.ps1) never hit this, which is what gave it away.

Usage:
    python install_autostart.py            # install
    python install_autostart.py uninstall  # remove
"""

from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STARTUP_DIR = Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
LNK_PATH = STARTUP_DIR / "Jarvis Voice Agent.lnk"
BAT_PATH = STARTUP_DIR / "JarvisVoiceAgent.bat"  # previous, broken approach -- cleaned up if present
def _pythonw() -> Path:
    venv = BASE_DIR / ".venv" / "Scripts" / "pythonw.exe"
    sibling = Path(sys.executable).with_name("pythonw.exe")  # installed layout: runtime\pythonw.exe
    return venv if venv.exists() else sibling if sibling.exists() else Path(sys.executable)


PYTHONW = _pythonw()
APP_PY = BASE_DIR / "app.py"
ICON_PATH = BASE_DIR / "assets" / "jarvis.ico"


def install() -> None:
    if not PYTHONW.exists():
        print(f"Не найден {PYTHONW}. Сначала создайте venv и установите зависимости (см. README.md).")
        return

    import win32com.client

    STARTUP_DIR.mkdir(parents=True, exist_ok=True)
    if BAT_PATH.exists():
        BAT_PATH.unlink()

    shell = win32com.client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortCut(str(LNK_PATH))
    shortcut.TargetPath = str(PYTHONW)
    shortcut.Arguments = f'"{APP_PY}"'
    shortcut.WorkingDirectory = str(BASE_DIR)
    if ICON_PATH.exists():
        shortcut.IconLocation = f"{ICON_PATH},0"
    shortcut.Description = "Запустить голосового ассистента Джарвис при входе в Windows"
    shortcut.Save()

    print(f"Автозапуск установлен: {LNK_PATH}")
    print("Джарвис будет запускаться при входе в Windows (иконка в трее).")


def uninstall() -> None:
    removed = False
    if LNK_PATH.exists():
        LNK_PATH.unlink()
        removed = True
    if BAT_PATH.exists():
        BAT_PATH.unlink()
        removed = True
    if removed:
        print("Автозапуск отключён.")
    else:
        print("Автозапуск не был установлен -- нечего удалять.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "uninstall":
        uninstall()
    else:
        install()
