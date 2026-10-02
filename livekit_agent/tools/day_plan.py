"""Voice control of the HUD panel (hud_panel.py): Jarvis's hologram face,
status, subtitles and the day plan as one full-screen page on a monitor.

The page and its live feed are served by hud_panel (normally inside the HUD
process); these tools only open/close the window. Nothing here calls an LLM.
"""

from __future__ import annotations

import asyncio

from livekit.agents import RunContext, function_tool

import dayplan_data
import hud_panel
from tools._logging import log_call
from tools.registry import register_impl, register_tool


def _minimize_windows_on(monitor: int) -> int:
    """Minimizes every normal top-level window whose centre is on `monitor` -> how many."""
    import ctypes
    from ctypes import wintypes

    import screens

    mon = screens.get(monitor)
    user32 = ctypes.windll.user32
    found: list[int] = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd) or user32.GetWindowTextLengthW(hwnd) == 0:
            return True
        if user32.GetWindow(hwnd, 4):                       # owned popups (tooltips, menus): skip
            return True
        ex = user32.GetWindowLongW(hwnd, -20)
        if ex & 0x80:                                        # WS_EX_TOOLWINDOW: HUD, tray bits
            return True
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
        if mon.left <= cx < mon.right and mon.top <= cy < mon.bottom:
            found.append(hwnd)
        return True

    with screens._dpi():
        user32.EnumWindows(proc(_cb), 0)
        for hwnd in found:
            user32.ShowWindow(hwnd, 6)                       # SW_MINIMIZE
    return len(found)


@register_impl("show_day_plan")
@log_call("show_day_plan")
async def _show_day_plan(*, monitor: int = 2) -> dict:
    import config
    import screens

    if config.HUD_WALLPAPER:
        count = len(await asyncio.to_thread(screens.monitors))
        target = 2 if count >= 2 else 1
        n = await asyncio.to_thread(_minimize_windows_on, target)
        what = f"свернул {n} окон" if n else "окна и так не мешают"
        return {"status": "ok", "message": f"План дня — на обоях монитора {target}, {what}."}

    count = len(await asyncio.to_thread(screens.monitors))
    note = " Второго монитора нет — открыл на основном." if monitor > count else ""
    try:
        await asyncio.to_thread(hud_panel.set_plan, True)       # the open page animates it in
        if await asyncio.to_thread(hud_panel.is_open):
            used = monitor if monitor <= count else 1
        else:
            used = await asyncio.to_thread(hud_panel.open_panel, monitor)
    except Exception as exc:
        return {"status": "error", "message": f"Не удалось открыть панель: {exc}"}
    data = await asyncio.to_thread(dayplan_data.collect)
    n = len(data["events"])
    events = f"событий сегодня: {n}" if n else "событий на сегодня нет"
    return {"status": "ok", "message": f"План дня открыт в панели на мониторе {used}: {events}, "
                                       f"дел: {len(data['todos'])}.{note}"}


@register_tool
@function_tool
async def show_day_plan(context: RunContext, monitor: int = 2) -> str:
    """Show the day plan (today's events, todos, timers, weather) -- it unfolds
    with an animation inside Jarvis's full-screen HUD panel next to his
    hologram face; opens that panel first if it isn't open:
    "открой план на день", "покажи расписание", "перейди на список дел". The
    face flies back into its orb and the plan page (3D day ring, what's next,
    todos, timers) opens out of it. It stays live. Say one short line after, don't read the plan out.

    Args:
        monitor: 2 = second monitor (default), 1 = main monitor.
    """
    result = await _show_day_plan(monitor=monitor)
    return result["message"]


@register_impl("close_day_plan")
@log_call("close_day_plan")
async def _close_day_plan() -> dict:
    await asyncio.to_thread(hud_panel.set_plan, False)
    return {"status": "ok", "message": "План убрал."}


@register_tool
@function_tool
async def close_day_plan(context: RunContext) -> str:
    """Fold the day plan away inside the panel; Jarvis's face stays ("закрой план", "убери план")."""
    result = await _close_day_plan()
    return result["message"]


@register_impl("show_face")
@log_call("show_face")
async def _show_face() -> dict:
    await asyncio.to_thread(hud_panel.set_face, True)
    return {"status": "ok", "message": "Нейрон раскрывается в лицо."}


@register_tool
@function_tool
async def show_face(context: RunContext) -> str:
    """Jarvis's main form on the HUD/wallpaper is a 3D neuron; this opens his
    hologram face in its place (the neuron folds into its orb, the face assembles):
    "покажи лицо", "открой лицо", "покажи своё лицо". Say one short line after."""
    result = await _show_face()
    return result["message"]


@register_impl("hide_face")
@log_call("hide_face")
async def _hide_face() -> dict:
    await asyncio.to_thread(hud_panel.set_face, False)
    return {"status": "ok", "message": "Лицо убрал, снова нейрон."}


@register_tool
@function_tool
async def hide_face(context: RunContext) -> str:
    """Put the face away and go back to the neuron: "убери лицо", "закрой лицо",
    "верни нейрон", "стань нейроном". Say one short line after."""
    result = await _hide_face()
    return result["message"]


@register_impl("open_hud_panel")
@log_call("open_hud_panel")
async def _open_hud_panel(*, monitor: int = 2) -> dict:
    try:
        if await asyncio.to_thread(hud_panel.is_open):
            return {"status": "ok", "message": "Панель уже открыта."}
        used = await asyncio.to_thread(hud_panel.open_panel, monitor)
    except Exception as exc:
        return {"status": "error", "message": f"Не удалось открыть панель: {exc}"}
    return {"status": "ok", "message": f"Панель открыта на мониторе {used}."}


@register_tool
@function_tool
async def open_hud_panel(context: RunContext, monitor: int = 2) -> str:
    """Open Jarvis's full-screen panel with his hologram face on a monitor, without
    the plan ("выведи себя на второй экран", "открой панель").

    Args:
        monitor: 2 = second monitor (default), 1 = main monitor.
    """
    result = await _open_hud_panel(monitor=monitor)
    return result["message"]


@register_impl("close_hud_panel")
@log_call("close_hud_panel")
async def _close_hud_panel() -> dict:
    killed = await asyncio.to_thread(hud_panel.close_panel)
    return {"status": "ok", "message": "Панель закрыта." if killed else "Панель и так не открыта."}


@register_tool
@function_tool
async def close_hud_panel(context: RunContext) -> str:
    """Close the whole full-screen panel window with the face ("закрой панель", "убери себя со второго экрана")."""
    result = await _close_hud_panel()
    return result["message"]
