"""System-level tools: launch/close apps, find files, screenshot, system_control, status.

`system_control` only ever dispatches on `config.SAFE_SYSTEM_ACTIONS` -- there
is no code path from an LLM argument to an arbitrary shell command here.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from typing import Literal

import psutil
from livekit.agents import RunContext, function_tool

import config
from tools import app_launcher
from tools._logging import log_call
from tools.registry import register_impl, register_tool

SYSTEM = config.SYSTEM


@register_impl("open_application")
@log_call("open_application")
async def _open_application(*, name: str) -> dict:
    # app_launcher escalates through PATH -> Windows "App Paths" registry ->
    # an indexed Start Menu shortcut -> typing into Start Menu search,
    # verifying a real process actually appeared at each step (see
    # tools/app_launcher.py) -- a bare os.startfile(name) only resolves the
    # handful of apps that register a PATH entry or a plain App Paths key,
    # which silently misreported real, installed apps (Discord confirmed
    # live: has a Start Menu shortcut and launches fine, but os.startfile
    # alone can't find it) as "not installed".
    ok, message = await app_launcher.launch(name)
    return {"status": "ok" if ok else "error", "message": message}


@register_tool
@function_tool
async def open_application(context: RunContext, name: str) -> str:
    """Open a desktop application by its common name.

    Args:
        name: The application's common/spoken name, e.g. "Chrome", "VS Code", "Telegram".
    """
    result = await _open_application(name=name)
    return result["message"]


def _find_process_hint(name: str) -> str:
    key = name.strip().lower()
    return config.APP_PROCESS_HINTS.get(key, key)


@register_impl("close_application")
@log_call("close_application")
async def _close_application(*, name: str) -> dict:
    hint = _find_process_hint(name)
    matched = []
    for proc in psutil.process_iter(["pid", "name"]):
        pname = (proc.info.get("name") or "").lower()
        if hint in pname:
            matched.append(proc)

    if not matched:
        return {"status": "error", "message": f"Не нашёл запущенное приложение «{name}»."}

    closed = []
    for proc in matched:
        try:
            proc.terminate()
            closed.append(proc.info["name"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    gone, alive = psutil.wait_procs(matched, timeout=3)
    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    return {"status": "ok", "message": f"Закрываю {name} ({len(closed)} процесс(ов))."}


@register_tool
@function_tool
async def close_application(context: RunContext, name: str) -> str:
    """Close a running application by its common name.

    Args:
        name: The application's common/spoken name, e.g. "Chrome", "Slack".
    """
    result = await _close_application(name=name)
    return result["message"]


@register_impl("find_and_open_file")
@log_call("find_and_open_file")
async def _find_and_open_file(*, query: str) -> dict:
    q = query.strip().lower()
    if not q:
        return {"status": "error", "message": "Пустой запрос для поиска файла."}

    def _scan() -> list[str]:
        hits: list[str] = []
        scanned = 0
        for base in config.FILE_SEARCH_DIRS:
            if not base.exists():
                continue
            for root, _dirs, files in os.walk(base):
                for fname in files:
                    scanned += 1
                    if scanned > config.FILE_SEARCH_MAX_SCAN:
                        return hits
                    if q in fname.lower():
                        hits.append(os.path.join(root, fname))
                        if len(hits) >= config.FILE_SEARCH_MAX_RESULTS * 4:
                            return hits
        return hits

    hits = await asyncio.to_thread(_scan)
    if not hits:
        return {"status": "not_found", "message": f"Файл, похожий на «{query}», не найден."}

    hits.sort(key=len)
    top = hits[: config.FILE_SEARCH_MAX_RESULTS]

    if len(top) > 1:
        listing = "; ".join(os.path.basename(p) for p in top)
        return {
            "status": "ambiguous",
            "message": f"Нашёл несколько файлов: {listing}. Уточните, какой открыть.",
            "candidates": top,
        }

    path = top[0]
    try:
        if SYSTEM == "Windows":
            os.startfile(path)  # type: ignore[attr-defined]
        elif SYSTEM == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return {"status": "ok", "message": f"Открываю файл {os.path.basename(path)}.", "path": path}
    except Exception as exc:
        return {"status": "error", "message": f"Нашёл файл, но не смог открыть: {exc}"}


@register_tool
@function_tool
async def find_and_open_file(context: RunContext, query: str) -> str:
    """Search common user folders (Desktop, Documents, Downloads) for a file and open it.

    If several files match, do not guess -- ask the user to be more specific.

    Args:
        query: A filename or part of a filename to search for.
    """
    result = await _find_and_open_file(query=query)
    return result["message"]


@register_impl("take_screenshot")
@log_call("take_screenshot")
async def _take_screenshot() -> dict:
    try:
        from PIL import ImageGrab
    except ImportError:
        return {"status": "error", "message": "Модуль для скриншотов (Pillow) не установлен."}

    def _grab() -> str:
        img = ImageGrab.grab()
        fname = time.strftime("screenshot_%Y%m%d_%H%M%S.png")
        path = config.SCREENSHOTS_DIR / fname
        img.save(path)
        return str(path)

    try:
        path = await asyncio.to_thread(_grab)
        return {"status": "ok", "message": f"Скриншот сохранён: {path}", "path": path}
    except Exception as exc:
        return {"status": "error", "message": f"Не удалось сделать скриншот: {exc}"}


@register_tool
@function_tool
async def take_screenshot(context: RunContext) -> str:
    """Capture the current screen and save it locally (never uploaded anywhere)."""
    result = await _take_screenshot()
    return result["message"]


# ---------------------------------------------------------------------------
# system_control
# ---------------------------------------------------------------------------

SystemAction = Literal["volume", "brightness", "wifi", "bluetooth", "lock", "sleep"]


def _set_volume(value: int) -> str:
    try:
        from ctypes import POINTER, cast

        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    except ImportError:
        return "Управление громкостью недоступно: не установлен pycaw."

    value = max(0, min(100, int(value)))
    devices = AudioUtilities.GetSpeakers()
    interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    volume = cast(interface, POINTER(IAudioEndpointVolume))
    volume.SetMasterVolumeLevelScalar(value / 100.0, None)
    return f"Громкость выставлена на {value}%."


def _set_brightness(value: int) -> str:
    try:
        import screen_brightness_control as sbc
    except ImportError:
        return "Управление яркостью недоступно: не установлен screen_brightness_control."

    value = max(0, min(100, int(value)))
    sbc.set_brightness(value)
    return f"Яркость выставлена на {value}%."


def _set_wifi(value: str) -> str:
    if SYSTEM != "Windows":
        return "Управление Wi-Fi реализовано только для Windows."
    enable = str(value).lower() in ("on", "true", "1", "enable", "включить")
    admin = "enabled" if enable else "disabled"
    try:
        subprocess.run(
            ["netsh", "interface", "set", "interface", "Wi-Fi", f"admin={admin}"],
            check=True,
            capture_output=True,
            timeout=15,
        )
        return f"Wi-Fi {'включён' if enable else 'выключен'}."
    except Exception as exc:
        return f"Не удалось изменить состояние Wi-Fi (нужны права администратора?): {exc}"


def _set_bluetooth(value: str) -> str:
    if SYSTEM != "Windows":
        return "Управление Bluetooth реализовано только для Windows."
    enable = str(value).lower() in ("on", "true", "1", "enable", "включить")
    cmdlet = "Enable-PnpDevice" if enable else "Disable-PnpDevice"
    ps_script = (
        "Get-PnpDevice -Class Bluetooth | "
        f"{cmdlet} -Confirm:$false -ErrorAction SilentlyContinue"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            check=True,
            capture_output=True,
            timeout=20,
        )
        return f"Bluetooth {'включён' if enable else 'выключен'} (нужны права администратора)."
    except Exception as exc:
        return f"Не удалось изменить состояние Bluetooth: {exc}"


def _lock_workstation() -> str:
    if SYSTEM == "Windows":
        import ctypes

        ctypes.windll.user32.LockWorkStation()  # type: ignore[attr-defined]
        return "Экран заблокирован."
    if SYSTEM == "Darwin":
        subprocess.run(
            ["/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession", "-suspend"]
        )
        return "Экран заблокирован."
    subprocess.run(["loginctl", "lock-session"])
    return "Экран заблокирован."


def _sleep_system() -> str:
    if SYSTEM == "Windows":
        subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
        return "Ухожу в спящий режим."
    if SYSTEM == "Darwin":
        subprocess.run(["pmset", "sleepnow"])
        return "Ухожу в спящий режим."
    subprocess.run(["systemctl", "suspend"])
    return "Ухожу в спящий режим."


@register_impl("system_control")
@log_call("system_control")
async def _system_control(*, action: str, value: object = None, confirm: bool = False) -> dict:
    if action not in config.SAFE_SYSTEM_ACTIONS:
        return {"status": "error", "message": f"Действие «{action}» не входит в разрешённый список."}

    if action not in config.SYSTEM_ACTIONS_NO_CONFIRM and not confirm:
        return {
            "status": "needs_confirmation",
            "message": (
                f"Действие «{action}» необратимо прерывает сеанс. "
                "Скажите «да, подтверждаю», чтобы выполнить."
            ),
        }

    def _run() -> str:
        if action == "volume":
            return _set_volume(int(value))  # type: ignore[arg-type]
        if action == "brightness":
            return _set_brightness(int(value))  # type: ignore[arg-type]
        if action == "wifi":
            return _set_wifi(str(value))
        if action == "bluetooth":
            return _set_bluetooth(str(value))
        if action == "lock":
            return _lock_workstation()
        if action == "sleep":
            return _sleep_system()
        return "Неизвестное действие."

    try:
        message = await asyncio.to_thread(_run)
        return {"status": "ok", "message": message}
    except Exception as exc:
        return {"status": "error", "message": f"Не удалось выполнить «{action}»: {exc}"}


@register_tool
@function_tool
async def system_control(
    context: RunContext,
    action: SystemAction,
    value: str | None = None,
    confirm: bool = False,
) -> str:
    """Change a whitelisted system setting: volume, brightness, wifi, bluetooth, lock, sleep.

    "sleep" suspends the machine and requires the user to have just said
    "да, подтверждаю" -- call once with confirm=False to get the confirmation
    prompt, then again with confirm=True only after they confirm out loud.

    Args:
        action: One of "volume", "brightness", "wifi", "bluetooth", "lock", "sleep".
        value: For "volume"/"brightness" an integer 0-100 as a string. For
            "wifi"/"bluetooth" either "on" or "off". Unused for "lock"/"sleep".
        confirm: Must be True to actually put the machine to sleep.
    """
    result = await _system_control(action=action, value=value, confirm=confirm)
    return result["message"]


@register_impl("get_system_status")
@log_call("get_system_status")
async def _get_system_status() -> dict:
    def _collect() -> dict:
        cpu = psutil.cpu_percent(interval=0.3)
        mem = psutil.virtual_memory()
        disk_path = "C:\\" if SYSTEM == "Windows" else "/"
        disk = psutil.disk_usage(disk_path)
        return {
            "cpu_percent": cpu,
            "ram_percent": mem.percent,
            "ram_used_gb": round(mem.used / 2**30, 1),
            "ram_total_gb": round(mem.total / 2**30, 1),
            "disk_percent": disk.percent,
            "disk_free_gb": round(disk.free / 2**30, 1),
            "disk_total_gb": round(disk.total / 2**30, 1),
        }

    stats = await asyncio.to_thread(_collect)
    message = (
        f"Загрузка процессора {stats['cpu_percent']:.0f}%, "
        f"память {stats['ram_percent']:.0f}% "
        f"({stats['ram_used_gb']} из {stats['ram_total_gb']} ГБ), "
        f"диск занят на {stats['disk_percent']:.0f}%, "
        f"свободно {stats['disk_free_gb']} ГБ."
    )
    return {"status": "ok", "message": message, **stats}


@register_tool
@function_tool
async def get_system_status(context: RunContext) -> str:
    """Report current CPU, RAM, and disk usage."""
    result = await _get_system_status()
    return result["message"]
