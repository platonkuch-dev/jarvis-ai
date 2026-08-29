#window_control.py
"""
LLM-facing tools for the Windows Control Layer (windows_control/).

Two tools, matching the two jobs the spec asks for:

  window_manager  — process/window level control (Level 1: native.py)
                     switch/close/minimize/maximize/restore/move/resize,
                     list windows/processes, wait for a window to appear.

  ui_automation   — control level, INSIDE a window (Level 2-4 ladder via
                     router.py): find/click/type/read/select UI elements,
                     inspect a bounded UI tree.

Both simply validate/parse `parameters` (same calling convention as every
other actions/*.py tool: `parameters`, `player`) and hand off to
windows_control.router, which does the actual layered work and logging.
"""
from __future__ import annotations

import platform

from windows_control import router

_OS = platform.system()

_UNSUPPORTED = (
    "The Windows Control Layer (window_manager / ui_automation) only supports "
    f"Windows — this machine is running {_OS}."
)


def launch_and_verify(app_name: str) -> str:
    """
    Wraps actions.open_app.open_app() with the PERCEIVE -> VERIFY step the
    plain launcher can't do on its own: check if the app is already running
    (skip a redundant launch), then after launching, actually wait for and
    focus a real window instead of just trusting the subprocess call — and
    record the result in the shared ExecutionContext so a follow-up
    window_manager/ui_automation call can default to "the app we just opened".
    Falls back to the plain launcher on non-Windows.
    """
    if not app_name:
        return "No application name provided."
    if _OS != "Windows":
        from actions.open_app import open_app as _plain_launch
        return _plain_launch(parameters={"app_name": app_name})

    from windows_control import app_resolver
    result = app_resolver.launch_or_focus(app_name)
    return result.message


def _int_or_none(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def window_manager(parameters: dict = None, response=None, player=None, session_memory=None) -> str:
    """
    parameters:
      action  : list_windows | list_processes | get_active | focus | switch_to |
                minimize | maximize | restore | close | close_app | move | resize |
                wait_for_window
      app     : app name or window title fragment (omit to use the last-focused app)
      x, y, width, height : ints, for move/resize
      timeout : seconds, for wait_for_window (default 10)
      force   : bool, force-kill on close if the app doesn't close gracefully
    """
    if _OS != "Windows":
        return _UNSUPPORTED

    params = parameters or {}
    action = (params.get("action") or "").strip().lower().replace(" ", "_")
    app = (params.get("app") or params.get("title") or params.get("target") or "").strip()

    if not action:
        return "No action specified for window_manager."
    if player:
        player.write_log(f"[WindowManager] {action} {app}".strip())

    try:
        if action == "list_windows":
            return router.get_windows(app).as_str()

        if action == "list_processes":
            return router.get_processes(app).as_str()

        if action == "get_active":
            return router.get_active_window_info().as_str()

        if action in ("focus", "switch_to", "switch", "activate"):
            return router.focus(app).as_str()

        if action == "minimize":
            return router.minimize(app).as_str()

        if action == "maximize":
            return router.maximize(app).as_str()

        if action == "restore":
            return router.restore(app).as_str()

        if action in ("close", "close_app", "quit", "terminate"):
            force = str(params.get("force", "")).lower() in ("true", "1", "yes")
            return router.close(app, force=force).as_str()

        if action == "move":
            return router.move(app, x=_int_or_none(params.get("x")), y=_int_or_none(params.get("y"))).as_str()

        if action == "resize":
            return router.resize(
                app,
                width=_int_or_none(params.get("width")),
                height=_int_or_none(params.get("height")),
            ).as_str()

        if action == "wait_for_window":
            timeout = float(params.get("timeout", 10.0))
            return router.wait_for_window(app, timeout=timeout).as_str()

        return f"Unknown window_manager action: '{action}'."

    except Exception as e:
        print(f"[WindowManager] Error ({action}): {e}")
        return f"window_manager '{action}' failed: {e}"


def ui_automation(parameters: dict = None, response=None, player=None, session_memory=None) -> str:
    """
    parameters:
      action       : find | click | double_click | right_click | type | read |
                     select | clear | get_tree | wait_for_element
      app          : app/window to scope the search to (omit to use the last-focused app)
      query        : element name / text / automation id to search for
      control_type : optional filter, e.g. Button, Edit, CheckBox, ListItem...
      text         : text to type (type action)
      item         : item name to select (select action, for combo/list boxes)
      clear_first  : bool, clear the field before typing (default true)
      index        : which match to use if several elements match (default 0)
      max_depth    : tree depth limit for get_tree (default 3)
      max_elements : element cap for get_tree (default 150)
      timeout      : seconds, for wait_for_element (default 8)
      x, y         : optional literal screen coordinates (click action, skips
                     UI Automation lookup and clicks directly)
    """
    if _OS != "Windows":
        return _UNSUPPORTED

    params = parameters or {}
    action = (params.get("action") or "").strip().lower().replace(" ", "_")
    app = (params.get("app") or params.get("window") or "").strip()
    query = (params.get("query") or params.get("target") or params.get("description") or "").strip()
    control_type = (params.get("control_type") or "").strip()

    if not action:
        return "No action specified for ui_automation."
    if player:
        player.write_log(f"[UIAutomation] {action} {query}".strip())

    try:
        if action == "find":
            index = int(params.get("index", 0) or 0)
            return router.find_element(query, app=app, control_type=control_type, index=index).as_str()

        if action in ("click", "left_click"):
            x, y = _int_or_none(params.get("x")), _int_or_none(params.get("y"))
            return router.click(query, app=app, control_type=control_type, x=x, y=y).as_str()

        if action == "double_click":
            x, y = _int_or_none(params.get("x")), _int_or_none(params.get("y"))
            return router.click(query, app=app, control_type=control_type, x=x, y=y, double=True).as_str()

        if action == "right_click":
            x, y = _int_or_none(params.get("x")), _int_or_none(params.get("y"))
            return router.click(query, app=app, control_type=control_type, x=x, y=y, button="right").as_str()

        if action in ("type", "type_text"):
            text = params.get("text", "")
            if not text:
                return "No text provided to type."
            clear_first = str(params.get("clear_first", "true")).lower() in ("true", "1", "yes")
            return router.type_into(text, query=query, app=app, control_type=control_type, clear_first=clear_first).as_str()

        if action == "read":
            return router.read_element(query, app=app, control_type=control_type).as_str()

        if action == "select":
            item = params.get("item", "")
            return router.select_element(query, item=item, app=app, control_type=control_type).as_str()

        if action == "clear":
            return router.clear_element(query, app=app, control_type=control_type).as_str()

        if action in ("get_tree", "get_ui_tree", "ui_tree"):
            max_depth = int(params.get("max_depth", 3) or 3)
            max_elements = int(params.get("max_elements", 150) or 150)
            result = router.get_ui_tree(app=app, max_depth=max_depth, name_filter=query, max_elements=max_elements)
            if not result.success:
                return result.message
            import json
            return json.dumps(result.data, ensure_ascii=False)[:4000]

        if action == "wait_for_element":
            timeout = float(params.get("timeout", 8.0))
            return router.wait_for_element(query, app=app, control_type=control_type, timeout=timeout).as_str()

        return f"Unknown ui_automation action: '{action}'."

    except Exception as e:
        print(f"[UIAutomation] Error ({action}): {e}")
        return f"ui_automation '{action}' failed: {e}"
