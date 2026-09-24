"""Shared helpers for Setup.exe and Uninstall.exe (Windows only, stdlib only).

Everything that talks to Windows is done without a visible console: child
processes get CREATE_NO_WINDOW, and PowerShell is fed an -EncodedCommand
(UTF-16) so Cyrillic paths and shortcut names never depend on a codepage.
"""

from __future__ import annotations

import base64
import ctypes
import os
import subprocess
import sys
import winreg
from ctypes import wintypes
from pathlib import Path

APP_NAME = "Jarvis AI"
APP_ID = "JarvisAI"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\JarvisAI"
MARKER = ".jarvisai-install"  # written into an install dir so upgrades only ever clean folders we made
KEEP_ON_UPGRADE = {"data", "logs", ".env"}  # user data lives inside app\ -- never part of the payload

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008

CSIDL_PROGRAMS = 0x0002
CSIDL_STARTUP = 0x0007
CSIDL_DESKTOPDIRECTORY = 0x0010


def default_install_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Programs" / APP_NAME


def exe_path() -> Path:
    buf = ctypes.create_unicode_buffer(32768)
    ctypes.windll.kernel32.GetModuleFileNameW(None, buf, len(buf))
    return Path(buf.value) if buf.value else Path(sys.argv[0]).resolve()


def known_folder(csidl: int) -> Path:
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
    return Path(buf.value)


def start_menu_dir() -> Path:
    return known_folder(CSIDL_PROGRAMS) / APP_NAME


def startup_shortcut() -> Path:
    return known_folder(CSIDL_STARTUP) / f"{APP_NAME}.lnk"


def desktop_shortcut() -> Path:
    return known_folder(CSIDL_DESKTOPDIRECTORY) / f"{APP_NAME}.lnk"


def run_hidden(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, creationflags=CREATE_NO_WINDOW, capture_output=True, timeout=timeout)


def powershell(script: str, timeout: float = 60) -> subprocess.CompletedProcess:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return run_hidden(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        timeout=timeout,
    )


def _q(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def create_shortcut(lnk: Path, target: Path, arguments: str, workdir: Path, icon: Path, description: str) -> None:
    lnk.parent.mkdir(parents=True, exist_ok=True)
    result = powershell(
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut(" + _q(lnk) + ");"
        "$s.TargetPath = " + _q(target) + ";"
        "$s.Arguments = " + _q(arguments) + ";"
        "$s.WorkingDirectory = " + _q(workdir) + ";"
        "$s.IconLocation = " + _q(f"{icon},0") + ";"
        "$s.Description = " + _q(description) + ";"
        "$s.Save()"
    )
    if result.returncode != 0 or not lnk.exists():
        raise RuntimeError(f"Не удалось создать ярлык {lnk.name}: {result.stderr.decode('utf-8', 'replace')[:200]}")


def kill_processes_in(directory: Path) -> None:
    """Stops anything running from the install folder (the tray app, workers, the panel) so its files can be replaced or removed.
    Never touches the process that is calling this: its own exe path is excluded, which also covers PyInstaller's second process."""
    powershell(
        "$d = " + _q(directory).lower() + ";"
        "$me = " + _q(exe_path()).lower() + ";"
        "Get-CimInstance Win32_Process | Where-Object {"
        " $_.ProcessId -ne $PID -and ((($_.ExecutablePath -as [string]).ToLower() -ne $me) -and ("
        " (($_.ExecutablePath -as [string]).ToLower().StartsWith($d)) -or (($_.CommandLine -as [string]).ToLower().Contains($d))"
        ")) } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }",
        timeout=90,
    )


def read_install_location() -> Path | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "InstallLocation")
            return Path(value) if value else None
    except OSError:
        return None


def write_uninstall_entry(install_dir: Path, version: str, size_kb: int) -> None:
    uninstaller = install_dir / "Uninstall.exe"
    values = {
        "DisplayName": APP_NAME,
        "DisplayVersion": version,
        "Publisher": APP_NAME,
        "InstallLocation": str(install_dir),
        "DisplayIcon": str(install_dir / "jarvis.ico"),
        "UninstallString": f'"{uninstaller}"',
        "QuietUninstallString": f'"{uninstaller}" /S',
    }
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        for name, value in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, int(size_kb))
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)


def delete_uninstall_entry() -> None:
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass


def vc_runtime_present() -> bool:
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    return all((system32 / name).exists() for name in ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"))


class _SHELLEXECUTEINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
    ]


def run_elevated(exe: Path, params: str) -> int:
    """Runs an installer that needs administrator rights (Windows shows its own consent prompt) and waits for it.
    Returns its exit code, or -1 if the user declined."""
    info = _SHELLEXECUTEINFO()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = str(exe)
    info.lpParameters = params
    info.nShow = 0
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        return -1
    ctypes.windll.kernel32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
    code = wintypes.DWORD()
    ctypes.windll.kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(info.hProcess)
    return int(code.value)


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def dir_size_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def rmtree_retry(path: Path, keep: set[str] | None = None, attempts: int = 8) -> None:
    """Deletes a folder's contents (except top-level names in `keep`), retrying while Windows releases file locks."""
    import shutil
    import time

    keep = keep or set()
    for attempt in range(attempts):
        leftovers = 0
        if not path.exists():
            return
        for entry in path.iterdir():
            if entry.name in keep:
                continue
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
            except OSError:
                leftovers += 1
        if not leftovers:
            return
        time.sleep(1.0 + attempt * 0.4)
    raise OSError(f"Не удалось удалить всё из {path} — возможно, какой-то файл занят программой.")
