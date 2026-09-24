"""LLM-facing tools for the Windows Control Layer (windows_control/).

  window_manager  — process/window level control (Level 1: native.py):
                     focus/close/minimize/maximize/restore/move/resize,
                     list windows/processes, wait for a window, or launch-
                     or-focus an app with real window verification.

ui_automation (find/click/type/read/select UI elements inside a window, via
router.py's Level 2-4 ladder) is intentionally NOT registered as an LLM-facing
tool below: its lower levels guess by vision or type blindly into whatever
has focus and still report success. The model gets tools/quick_ui.py
instead, which uses only the strict UI Automation level and hands anything
else to use_computer. _ui_automation stays registered in IMPL_REGISTRY
(@register_impl only, no @function_tool) so a previously-saved scenario
that recorded a step against it still replays.

Both are thin, logged wrappers around windows_control.router, which does the
actual layered work (native API -> UI Automation -> keyboard/mouse -> vision)
and its own structured logging. All calls are blocking Win32/UIA/pyautogui
work, so they run on a thread via asyncio.to_thread instead of blocking the
event loop.
"""

from __future__ import annotations

import asyncio
import json
from typing import Literal

from livekit.agents import RunContext, function_tool

import config
from tools._logging import log_call
from tools.registry import register_impl, register_tool

_UNSUPPORTED = (
    "Управление окнами (window_manager / ui_automation) поддерживается только "
    f"на Windows — эта машина работает на {config.SYSTEM}."
)

WindowAction = Literal[
    "list_windows", "list_processes", "get_active", "focus", "minimize",
    "maximize", "restore", "close", "move", "resize", "wait_for_window",
    "launch_or_focus",
]


@register_impl("window_manager")
@log_call("window_manager")
async def _window_manager(
    *,
    action: str,
    app: str = "",
    x: int | None = None,
    y: int | None = None,
    width: int | None = None,
    height: int | None = None,
    timeout: float = 10.0,
    force: bool = False,
) -> dict:
    if config.SYSTEM != "Windows":
        return {"status": "error", "message": _UNSUPPORTED}

    from windows_control import app_resolver, router

    def _run():
        if action == "list_windows":
            return router.get_windows(app)
        if action == "list_processes":
            return router.get_processes(app)
        if action == "get_active":
            return router.get_active_window_info()
        if action == "focus":
            return router.focus(app)
        if action == "minimize":
            return router.minimize(app)
        if action == "maximize":
            return router.maximize(app)
        if action == "restore":
            return router.restore(app)
        if action == "close":
            return router.close(app, force=force)
        if action == "move":
            return router.move(app, x=x, y=y)
        if action == "resize":
            return router.resize(app, width=width, height=height)
        if action == "wait_for_window":
            return router.wait_for_window(app, timeout=timeout)
        if action == "launch_or_focus":
            result = app_resolver.launch_or_focus(app, wait_timeout=timeout)
            return router.ActionResult(result.success, result.message, "native")
        return None

    result = await asyncio.to_thread(_run)
    if result is None:
        return {"status": "error", "message": f"Неизвестное действие window_manager: «{action}»."}
    return {"status": "ok" if result.success else "error", "message": result.message}


@register_tool
@function_tool
async def window_manager(
    context: RunContext,
    action: WindowAction,
    app: str = "",
    x: int | None = None,
    y: int | None = None,
    width: int | None = None,
    height: int | None = None,
    timeout: float = 10.0,
    force: bool = False,
) -> str:
    """Control windows and processes at the OS level (Windows only).

    Args:
        action: "list_windows"/"list_processes" to enumerate; "get_active" for
            the foreground window; "focus"/"minimize"/"maximize"/"restore"/"close"
            to act on a window; "move"/"resize" with x/y or width/height;
            "wait_for_window" to poll until one appears; "launch_or_focus" to
            open an app if it isn't running yet, or bring it to front if it is
            (verifies a real window shows up, unlike a plain "open app").
        app: App name or window title fragment. Omit to use the last-focused
            window from a previous call in this conversation.
        x: Target left position, for "move".
        y: Target top position, for "move".
        width: Target width, for "resize".
        height: Target height, for "resize".
        timeout: Seconds to wait, for "wait_for_window"/"launch_or_focus".
        force: For "close" — force-kill if the app doesn't close gracefully
            within a few seconds (never applies to protected system processes).
    """
    result = await _window_manager(
        action=action, app=app, x=x, y=y, width=width, height=height,
        timeout=timeout, force=force,
    )
    return result["message"]


@register_impl("ui_automation")
@log_call("ui_automation")
async def _ui_automation(
    *,
    action: str,
    app: str = "",
    query: str = "",
    control_type: str = "",
    text: str = "",
    item: str = "",
    clear_first: bool = True,
    index: int = 0,
    max_depth: int = 3,
    max_elements: int = 150,
    timeout: float = 8.0,
    x: int | None = None,
    y: int | None = None,
) -> dict:
    if config.SYSTEM != "Windows":
        return {"status": "error", "message": _UNSUPPORTED}

    from windows_control import router

    def _run():
        if action == "find":
            return router.find_element(query, app=app, control_type=control_type, index=index)
        if action == "click":
            return router.click(query, app=app, control_type=control_type, x=x, y=y)
        if action == "double_click":
            return router.click(query, app=app, control_type=control_type, x=x, y=y, double=True)
        if action == "right_click":
            return router.click(query, app=app, control_type=control_type, x=x, y=y, button="right")
        if action == "type":
            if not text:
                return "no_text"
            return router.type_into(text, query=query, app=app, control_type=control_type, clear_first=clear_first)
        if action == "read":
            return router.read_element(query, app=app, control_type=control_type)
        if action == "select":
            return router.select_element(query, item=item, app=app, control_type=control_type)
        if action == "clear":
            return router.clear_element(query, app=app, control_type=control_type)
        if action == "get_tree":
            return router.get_ui_tree(app=app, max_depth=max_depth, name_filter=query, max_elements=max_elements)
        if action == "wait_for_element":
            return router.wait_for_element(query, app=app, control_type=control_type, timeout=timeout)
        return None

    result = await asyncio.to_thread(_run)

    if result is None:
        return {"status": "error", "message": f"Неизвестное действие ui_automation: «{action}»."}
    if result == "no_text":
        return {"status": "error", "message": "Не указан текст для ввода."}

    if action == "get_tree":
        if not result.success:
            return {"status": "error", "message": result.message}
        return {"status": "ok", "message": json.dumps(result.data, ensure_ascii=False)[:4000]}

    return {"status": "ok" if result.success else "error", "message": result.message}


# No @register_tool/@function_tool wrapper here on purpose -- see the module
# docstring: ui_automation is deliberately not offered to the model; single
# clicks/types/reads go through tools/quick_ui.py (strict UIA, then use_computer).
