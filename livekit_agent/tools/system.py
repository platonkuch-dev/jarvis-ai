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
    """Open a desktop application by its common name. Also opens web services
    named like an app ("YouTube", "Gmail", "vk.com") in the default browser.

    Args:
        name: The application's common/spoken name, e.g. "Chrome", "VS Code", "Telegram", "Диспетчер задач".
    """
    result = await _open_application(name=name)
    return result["message"]


_NEVER_CLOSE = {
    "explorer.exe", "dwm.exe", "csrss.exe", "winlogon.exe", "lsass.exe", "services.exe",
    "svchost.exe", "smss.exe", "wininit.exe", "system", "python.exe", "pythonw.exe",
}


def _find_process_hint(name: str) -> str:
    """Spoken name -> process-name substring: explicit hint ("гугл" ->
    chrome), else the launcher alias's exe ("ворд" -> winword), else the
    name itself minus ".exe" ("Taskmgr.exe" -> taskmgr)."""
    key = name.strip().lower()
    if key in config.APP_PROCESS_HINTS:
        return config.APP_PROCESS_HINTS[key]
    alias = config.APP_ALIASES.get(key, {}).get(SYSTEM, "")
    if alias and ":" not in alias:
        return alias.lower().removesuffix(".exe").split()[0]
    return key.removesuffix(".exe")


@register_impl("close_application")
@log_call("close_application")
async def _close_application(*, name: str) -> dict:
    hint = _find_process_hint(name)
    # A substring match on a 1-2 letter hint ("e", "co") would terminate half
    # the system; the processes below keep Windows / Jarvis itself alive.
    if len(hint) < 3:
        return {"status": "error", "message": f"Слишком короткое название «{name}» — уточните, что закрыть."}
    matched = []
    for proc in psutil.process_iter(["pid", "name"]):
        pname = (proc.info.get("name") or "").lower()
        if hint in pname and pname not in _NEVER_CLOSE and proc.pid != os.getpid():
            matched.append(proc)

    if not matched:
        return {"status": "error", "message": f"Не нашёл запущенное приложение «{name}»."}

    closed, denied = [], []
    for proc in matched:
        try:
            proc.terminate()
            closed.append(proc)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            denied.append(proc)

    gone, alive = psutil.wait_procs(closed, timeout=3)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            denied.append(proc)

    # Elevated apps (Task Manager, anything "run as administrator") refuse a
    # non-elevated terminate -- say so instead of reporting a close that
    # never happened.
    if denied and not gone:
        return {"status": "error",
                "message": f"Не могу закрыть {name}: оно запущено с правами администратора, "
                           "а у меня их нет."}
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
async def _take_screenshot(*, monitor: int = 0) -> dict:
    try:
        import screens
    except ImportError:
        return {"status": "error", "message": "Модуль для скриншотов (Pillow) не установлен."}

    def _grab() -> str:
        img = screens.grab(int(monitor or 0))
        which = f"_m{monitor}" if monitor else ""
        fname = time.strftime(f"screenshot_%Y%m%d_%H%M%S{which}.png")
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
async def take_screenshot(context: RunContext, monitor: int = 0) -> str:
    """Capture the screen and save it locally (never uploaded anywhere).
    To SEE what is on a screen use look_at_screen instead.

    Args:
        monitor: 0 = all monitors in one image (default), 1 = main, 2 = second monitor.
    """
    result = await _take_screenshot(monitor=monitor)
    return result["message"]


# ---------------------------------------------------------------------------
# system_control
# ---------------------------------------------------------------------------

SystemAction = Literal["volume", "brightness", "wifi", "bluetooth", "lock", "mute", "sleep", "shutdown", "restart"]


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


def _toggle_mute() -> str:
    try:
        from ctypes import POINTER, cast

        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    except ImportError:
        return "Управление звуком недоступно: не установлен pycaw."

    interface = AudioUtilities.GetSpeakers().Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    volume = cast(interface, POINTER(IAudioEndpointVolume))
    muted = not bool(volume.GetMute())
    volume.SetMute(int(muted), None)
    return "Звук выключен." if muted else "Звук включён."


def _power_off(restart: bool) -> str:
    # 15 s grace so the spoken reply finishes; "shutdown /a" cancels it.
    if SYSTEM == "Windows":
        subprocess.run(["shutdown", "/r" if restart else "/s", "/t", "15"], check=True)
    elif SYSTEM == "Darwin":
        subprocess.run(["osascript", "-e", f'tell app "System Events" to {"restart" if restart else "shut down"}'])
    else:
        subprocess.run(["systemctl", "reboot" if restart else "poweroff"])
    what = "Перезагружаю" if restart else "Выключаю"
    return f"{what} компьютер через 15 секунд. Отменить — «shutdown /a» в терминале."


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
                f"Действие «{action}» прерывает сеанс (несохранённая работа может пропасть). "
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
        if action == "mute":
            return _toggle_mute()
        if action == "sleep":
            return _sleep_system()
        if action in ("shutdown", "restart"):
            return _power_off(restart=action == "restart")
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
    """Change a whitelisted system setting: volume, brightness, wifi, bluetooth,
    lock, mute (toggle sound), sleep, shutdown, restart.

    "sleep", "shutdown" and "restart" require the user to have just said
    "да, подтверждаю" -- call once with confirm=False to get the confirmation
    prompt, then again with confirm=True only after they confirm out loud.

    Args:
        action: One of "volume", "brightness", "wifi", "bluetooth", "lock", "mute",
            "sleep", "shutdown", "restart".
        value: For "volume"/"brightness" an integer 0-100 as a string. For
            "wifi"/"bluetooth" either "on" or "off". Unused otherwise.
        confirm: Must be True to actually sleep / shut down / restart.
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
        battery = psutil.sensors_battery()
        return {
            "battery_percent": round(battery.percent) if battery else None,
            "battery_plugged": bool(battery.power_plugged) if battery else None,
            "uptime_h": round((time.time() - psutil.boot_time()) / 3600, 1),
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
        f"свободно {stats['disk_free_gb']} ГБ, "
        f"компьютер работает {stats['uptime_h']} ч."
    )
    if stats["battery_percent"] is not None:
        charging = "заряжается" if stats["battery_plugged"] else "от батареи"
        message += f" Батарея {stats['battery_percent']}% ({charging})."
    return {"status": "ok", "message": message, **stats}


@register_tool
@function_tool
async def get_system_status(context: RunContext) -> str:
    """Report current CPU, RAM, disk usage, uptime and battery."""
    result = await _get_system_status()
    return result["message"]
