"""
Action Router — the single place that decides HOW an action gets done.

The LLM never touches Windows directly: it calls a tool (actions/window_control.py),
which calls into this router with a structured action, and the router walks
the priority ladder for that action type:

    Native API (Level 1) -> UI Automation (Level 2) -> Keyboard/Mouse (Level 3) -> Vision (Level 4)

stopping at the first method that succeeds, logging every attempt
(logging_utils.log_action), and updating the shared ExecutionContext so
follow-up commands ("click it", "type into it") can resolve without the
caller re-specifying everything.

Every public function here returns an ActionResult — never raises for
"expected" failures (element not found, app not running, etc.) — so the
tool layer can always hand the LLM back a clear, spoken-friendly message
instead of a stack trace.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import native, uia, vision, keyboard_mouse
from .context import get_context, ElementRef
from .logging_utils import log_action

_AMBIGUOUS_TARGETS = {"", "it", "this", "that", "the button", "её", "его", "это", "him", "her"}


@dataclass
class ActionResult:
    success: bool
    message: str
    method: str = ""
    data: dict | None = field(default=None)

    def as_str(self) -> str:
        return self.message


# ── helpers ──────────────────────────────────────────────────────────────

def _resolve_scope(app: str = "") -> tuple:
    """Return (window_hint_for_native, app_label) — falls back to the last
    active app/window recorded in context when the caller didn't specify one."""
    ctx = get_context()
    if app:
        return app, app
    active = ctx.get_active()
    if active["app"] or active["window_title"]:
        return (active["window_title"] or active["app"]), (active["app"] or active["window_title"])
    return "", ""


def _target_window_info(app: str = "") -> native.WindowInfo | None:
    """
    Resolve which window an action should act on.

    If the caller named a specific app and it isn't found, this returns None
    — NOT the currently active window. Silently substituting "whatever's in
    the foreground" for a named-but-missing app would mean a mistyped or
    already-closed app name could cause window_manager to close/minimize/
    move a completely unrelated window. Falling back to the active window is
    only safe when the caller genuinely didn't specify one (bare "minimize
    the window" / a follow-up with no app named, resolved via context).
    """
    if app:
        matches = native.find_windows(query=app)
        return matches[0] if matches else None

    scope, _ = _resolve_scope(app)
    if scope:
        matches = native.find_windows(query=scope)
        if matches:
            return matches[0]
    return native.get_active_window()


def _resolve_uia_window(app: str = ""):
    winfo = _target_window_info(app)
    if not winfo:
        return None, None
    win = uia.resolve_window(hwnd=winfo.hwnd)
    return win, winfo


def _label(app: str) -> str:
    return app or "(active window)"


# ── window management (Level 1) ─────────────────────────────────────────

def get_windows(query: str = "") -> ActionResult:
    t0 = time.monotonic()
    windows = native.find_windows(query=query) if query else native.list_windows()
    data = {"windows": [w.as_dict() for w in windows]}
    log_action("get_windows", target=query, method="Native", result="SUCCESS", elapsed=time.monotonic() - t0)
    if not windows:
        return ActionResult(True, "No matching windows found." if query else "No open windows.", "native", data)
    lines = [f"- {w.title} ({w.process_name}, pid {w.pid}{', minimized' if w.is_minimized else ''})" for w in windows[:20]]
    return ActionResult(True, f"{len(windows)} window(s):\n" + "\n".join(lines), "native", data)


def get_processes(query: str = "") -> ActionResult:
    t0 = time.monotonic()
    procs = native.list_processes(name_filter=query)
    data = {"processes": [p.as_dict() for p in procs]}
    log_action("get_processes", target=query, method="Native", result="SUCCESS", elapsed=time.monotonic() - t0)
    if not procs:
        return ActionResult(True, f"No running process matches '{query}'." if query else "No processes.", "native", data)
    lines = [f"- {p.name} (pid {p.pid}, {p.memory_mb} MB)" for p in procs[:20]]
    return ActionResult(True, f"{len(procs)} process(es):\n" + "\n".join(lines), "native", data)


def get_active_window_info() -> ActionResult:
    w = native.get_active_window()
    if not w:
        return ActionResult(False, "Could not determine the active window.", "native")
    return ActionResult(True, f"Active window: {w.title} ({w.process_name})", "native", {"window": w.as_dict()})


def _window_op(action_name: str, app: str, fn) -> ActionResult:
    t0 = time.monotonic()
    ctx = get_context()
    ctx.begin_action(action_name)
    winfo = _target_window_info(app)
    if not winfo:
        result = ActionResult(False, f"No window found for '{_label(app)}'.", "native")
        log_action(action_name, target=app, method="Native", result="FAILED", elapsed=time.monotonic() - t0)
        return result

    ok = fn(winfo.hwnd)
    elapsed = time.monotonic() - t0
    log_action(action_name, target=app, app=winfo.process_name, window=winfo.title,
               method="Native", result="SUCCESS" if ok else "FAILED", elapsed=elapsed)
    if ok:
        ctx.set_active(app=app or winfo.process_name, window_title=winfo.title,
                        hwnd=winfo.hwnd, pid=winfo.pid)
    ctx.record_history({"action": action_name, "target": app, "success": ok})
    verb = action_name.replace("_", " ")
    msg = f"{winfo.title} — {verb} {'done' if ok else 'failed'}."
    return ActionResult(ok, msg, "native", {"window": winfo.as_dict()})


def focus(app: str = "") -> ActionResult:
    return _window_op("focus_window", app, native.focus_window)


def minimize(app: str = "") -> ActionResult:
    return _window_op("minimize_window", app, native.minimize_window)


def maximize(app: str = "") -> ActionResult:
    return _window_op("maximize_window", app, native.maximize_window)


def restore(app: str = "") -> ActionResult:
    return _window_op("restore_window", app, native.restore_window)


def move(app: str = "", x: int | None = None, y: int | None = None) -> ActionResult:
    return _window_op("move_window", app, lambda hwnd: native.move_resize_window(hwnd, x=x, y=y))


def resize(app: str = "", width: int | None = None, height: int | None = None) -> ActionResult:
    return _window_op("resize_window", app, lambda hwnd: native.move_resize_window(hwnd, width=width, height=height))


def close(app: str = "", force: bool = False) -> ActionResult:
    t0 = time.monotonic()
    ctx = get_context()
    ctx.begin_action("close_window")
    winfo = _target_window_info(app)
    if not winfo:
        log_action("close_window", target=app, method="Native", result="FAILED", elapsed=time.monotonic() - t0)
        return ActionResult(False, f"No window found for '{_label(app)}'.", "native")

    native.close_window(winfo.hwnd)
    # wait_for_window_gone() polls every 0.3s and returns the instant the
    # window disappears -- these timeouts are only a worst-case ceiling for
    # apps that ignore/delay WM_CLOSE, not a fixed sleep, so a normal fast
    # close is unaffected by their size. Measured close-to-1.5s (was 3.0s)
    # + retry-to-1.0s (was 2.0s) halves the worst case (~5s -> ~2.5s,
    # matching a real observed max=5172ms close) for a voice command that
    # blocks on this before Gemini's function_response can go out, while
    # still giving a legitimately slow-but-successful close a fair chance.
    gone = native.wait_for_window_gone(winfo.hwnd, timeout=1.5)
    method = "Native (WM_CLOSE)"

    if not gone:
        # Some apps ignore a WM_CLOSE sent from a background process until
        # their window is foregrounded. Retry once before assuming a save
        # prompt is blocking the graceful close.
        if native.focus_window(winfo.hwnd):
            native.close_window(winfo.hwnd)
            gone = native.wait_for_window_gone(winfo.hwnd, timeout=1.0)
            method = "Native (WM_CLOSE retry)"

    if not gone:
        # App is probably showing an "unsaved changes?" prompt — that's a
        # legitimate reason to NOT force-kill it. Only fall through to a
        # harder close if explicitly asked.
        if force:
            ok, msg = native.kill_process(winfo.pid, force=True)
            method = "Native (kill_process, forced)"
            elapsed = time.monotonic() - t0
            log_action("close_window", target=app, app=winfo.process_name, window=winfo.title,
                       method=method, result="SUCCESS" if ok else "FAILED", elapsed=elapsed)
            return ActionResult(ok, msg, method)
        elapsed = time.monotonic() - t0
        log_action("close_window", target=app, app=winfo.process_name, window=winfo.title,
                   method=method, result="PENDING", elapsed=elapsed,
                   extra="Window still open — may be prompting to save.")
        return ActionResult(True, f"Asked {winfo.title} to close — it may be prompting to save changes.", method)

    elapsed = time.monotonic() - t0
    log_action("close_window", target=app, app=winfo.process_name, window=winfo.title,
               method=method, result="SUCCESS", elapsed=elapsed)
    ctx.record_history({"action": "close_window", "target": app, "success": True})
    return ActionResult(True, f"Closed {winfo.title}.", method)


def wait_for_window(app: str, timeout: float = 10.0) -> ActionResult:
    t0 = time.monotonic()
    w = native.wait_for_window(query=app, timeout=timeout)
    elapsed = time.monotonic() - t0
    log_action("wait_for_window", target=app, method="Native", result="SUCCESS" if w else "TIMEOUT", elapsed=elapsed)
    if w:
        get_context().set_active(app=app, window_title=w.title, hwnd=w.hwnd, pid=w.pid)
        return ActionResult(True, f"{w.title} appeared after {elapsed:.1f}s.", "native", {"window": w.as_dict()})
    return ActionResult(False, f"No window for '{app}' appeared within {timeout:.0f}s.", "native")


# ── UI element interaction (Level 2 -> 3 -> 4 ladder) ───────────────────

def _pick_query(query: str) -> str:
    if query and query.strip().lower() in _AMBIGUOUS_TARGETS:
        last = get_context().last_element()
        if last:
            return last.name
    return query


def find_element(query: str, app: str = "", control_type: str = "", index: int = 0) -> ActionResult:
    t0 = time.monotonic()
    query = _pick_query(query)
    win, winfo = _resolve_uia_window(app)
    app_label = winfo.process_name if winfo else app

    el = uia.find_element(win, query=query, control_type=control_type, index=index) if win else None
    if el is not None:
        ref = uia.element_to_ref(el, app=app_label, window_title=winfo.title if winfo else "")
        get_context().remember_elements([ref])
        elapsed = time.monotonic() - t0
        log_action("find_element", target=query, app=app_label, window=winfo.title if winfo else "",
                   method="UI Automation", result="SUCCESS", elapsed=elapsed)
        return ActionResult(True, f"Found '{query}' ({ref.control_type}) at {ref.center()}.",
                             "uia", {"element": ref.__dict__})

    # Level 4 fallback: vision, scoped to the window's rectangle if we have one.
    region = winfo.rect if winfo else None
    coords = vision.locate_element(query, region=region)
    elapsed = time.monotonic() - t0
    if coords:
        ref = ElementRef(name=query, app=app_label, window_title=winfo.title if winfo else "",
                          rect=(coords[0] - 4, coords[1] - 4, coords[0] + 4, coords[1] + 4), found_via="vision")
        get_context().remember_elements([ref])
        log_action("find_element", target=query, app=app_label, window=winfo.title if winfo else "",
                   method="UI Automation -> Vision", result="SUCCESS", elapsed=elapsed)
        return ActionResult(True, f"Found '{query}' via vision at {coords}.", "vision", {"element": ref.__dict__})

    log_action("find_element", target=query, app=app_label, window=winfo.title if winfo else "",
               method="UI Automation -> Vision", result="FAILED", elapsed=elapsed)
    return ActionResult(False, f"Could not find '{query}'.", None)


def click(query: str = "", app: str = "", control_type: str = "",
          x: int | None = None, y: int | None = None,
          double: bool = False, button: str = "left", strict: bool = False) -> ActionResult:
    """strict=True stops after Level 2: only a real UI Automation hit counts,
    no coordinate/vision guessing (tools/quick_ui.py falls back to the
    screen-reading agent instead)."""
    t0 = time.monotonic()
    ctx = get_context()
    ctx.begin_action("click")
    query = _pick_query(query)
    win, winfo = _resolve_uia_window(app)
    app_label = winfo.process_name if winfo else app
    window_title = winfo.title if winfo else ""

    # Level 2 — UI Automation. A bare control_type (no name) is a valid
    # search on its own — e.g. "the Document/Edit control in this window" —
    # so this must not require `query` to be non-empty.
    if (query or control_type) and win is not None:
        el = uia.find_element(win, query=query, control_type=control_type)
        if el is not None:
            res = uia.click_element(el, double=double, button=button)
            elapsed = time.monotonic() - t0
            log_action("click", target=query, app=app_label, window=window_title,
                       method="UI Automation", result="SUCCESS" if res.ok else "FAILED", elapsed=elapsed)
            if res.ok:
                ctx.remember_elements([uia.element_to_ref(el, app=app_label, window_title=window_title)])
                ctx.record_history({"action": "click", "target": query, "method": "uia", "success": True})
                return ActionResult(True, f"Clicked '{query}'.", "uia")
    if strict:
        return ActionResult(False, f"'{query}' not found by UI Automation.")

    # Level 3 — explicit coordinates given, use them directly.
    if x is not None and y is not None:
        try:
            keyboard_mouse.click(x, y, button=button, double=double)
            elapsed = time.monotonic() - t0
            log_action("click", target=f"({x},{y})", app=app_label, window=window_title,
                       method="Mouse", result="SUCCESS", elapsed=elapsed)
            ctx.record_history({"action": "click", "target": f"({x},{y})", "method": "mouse", "success": True})
            return ActionResult(True, f"Clicked at ({x}, {y}).", "mouse")
        except Exception as e:
            log_action("click", target=f"({x},{y})", app=app_label, window=window_title,
                       method="Mouse", result="FAILED", elapsed=time.monotonic() - t0, extra=str(e))

    # Level 4 — vision, scoped to the target window if known.
    if query:
        region = winfo.rect if winfo else None
        coords = vision.locate_element(query, region=region)
        elapsed = time.monotonic() - t0
        if coords:
            try:
                keyboard_mouse.click(*coords, button=button, double=double)
                log_action("click", target=query, app=app_label, window=window_title,
                           method="UI Automation -> Vision", result="SUCCESS", elapsed=elapsed)
                ctx.remember_elements([ElementRef(name=query, app=app_label, window_title=window_title,
                                                   rect=(coords[0]-4, coords[1]-4, coords[0]+4, coords[1]+4),
                                                   found_via="vision")])
                ctx.record_history({"action": "click", "target": query, "method": "vision", "success": True})
                return ActionResult(True, f"Clicked '{query}' via vision.", "vision")
            except Exception as e:
                log_action("click", target=query, app=app_label, window=window_title,
                           method="Vision", result="FAILED", elapsed=time.monotonic() - t0, extra=str(e))

    elapsed = time.monotonic() - t0
    log_action("click", target=query or f"({x},{y})", app=app_label, window=window_title,
               method="UI Automation -> Mouse -> Vision", result="FAILED", elapsed=elapsed)
    ctx.record_history({"action": "click", "target": query, "success": False})
    return ActionResult(False, f"Could not click '{query or (x, y)}' — not found by UI Automation or vision.")


def type_into(text: str, query: str = "", app: str = "", control_type: str = "",
              clear_first: bool = True, strict: bool = False) -> ActionResult:
    """strict=True: only type into a field UI Automation actually found --
    never fall through to vision or to blind typing at whatever has focus
    (which reports success even when the text lands in the wrong place)."""
    t0 = time.monotonic()
    ctx = get_context()
    ctx.begin_action("type")
    query = _pick_query(query)
    win, winfo = _resolve_uia_window(app)
    app_label = winfo.process_name if winfo else app
    window_title = winfo.title if winfo else ""

    # Level 2 — target a specific field via UI Automation. A bare
    # control_type (no name) is a valid search on its own.
    if (query or control_type) and win is not None:
        el = uia.find_element(win, query=query, control_type=control_type)
        if el is None and not control_type:
            # The caller's label often isn't the control's real name ("документ"
            # vs Notepad's "Text editor"); a window with exactly one text area
            # leaves no doubt where the text goes.
            el = uia.find_single_editable(win)
        if el is not None:
            res = uia.type_into_element(el, text, clear_first=clear_first)
            elapsed = time.monotonic() - t0
            log_action("type", target=query or control_type, app=app_label, window=window_title,
                       method="UI Automation", result="SUCCESS" if res.ok else "FAILED", elapsed=elapsed)
            if res.ok:
                ctx.record_history({"action": "type", "target": query, "method": "uia", "success": True})
                return ActionResult(True, f"Typed into '{query or control_type}'.", "uia")
    if strict:
        return ActionResult(False, f"Field '{query or control_type}' not found by UI Automation.")

    # Level 4 -> 3 — vision finds the field (needs a real text description,
    # a bare control_type means nothing to a vision model), keyboard types into it.
    if query:
        region = winfo.rect if winfo else None
        coords = vision.locate_element(query, region=region)
        if coords:
            try:
                keyboard_mouse.click(*coords)
                time.sleep(0.15)
                if clear_first:
                    keyboard_mouse.clear_field()
                keyboard_mouse.paste_text(text)
                elapsed = time.monotonic() - t0
                log_action("type", target=query, app=app_label, window=window_title,
                           method="Vision -> Keyboard", result="SUCCESS", elapsed=elapsed)
                ctx.record_history({"action": "type", "target": query, "method": "vision+keyboard", "success": True})
                return ActionResult(True, f"Typed into '{query}' (located via vision).", "vision+keyboard")
            except Exception as e:
                log_action("type", target=query, app=app_label, window=window_title,
                           method="Vision -> Keyboard", result="FAILED", elapsed=time.monotonic() - t0, extra=str(e))

    # Level 3 — blind type at whatever currently has focus.
    try:
        if clear_first and not query:
            pass  # blind clear is too destructive without a known target — skip
        keyboard_mouse.paste_text(text)
        elapsed = time.monotonic() - t0
        log_action("type", target=query or "(focused control)", app=app_label, window=window_title,
                   method="Keyboard", result="SUCCESS", elapsed=elapsed)
        ctx.record_history({"action": "type", "target": query, "method": "keyboard", "success": True})
        return ActionResult(True, "Typed at the current cursor position.", "keyboard")
    except Exception as e:
        elapsed = time.monotonic() - t0
        log_action("type", target=query, app=app_label, window=window_title,
                   method="Keyboard", result="FAILED", elapsed=elapsed, extra=str(e))
        return ActionResult(False, f"Could not type: {e}")


def read_element(query: str, app: str = "", control_type: str = "") -> ActionResult:
    t0 = time.monotonic()
    query = _pick_query(query)
    win, winfo = _resolve_uia_window(app)
    app_label = winfo.process_name if winfo else app
    el = uia.find_element(win, query=query, control_type=control_type) if win else None
    elapsed = time.monotonic() - t0
    if el is None:
        log_action("read_element", target=query, app=app_label, method="UI Automation", result="FAILED", elapsed=elapsed)
        return ActionResult(False, f"Could not find '{query}' to read.")
    info = uia.read_element(el)
    log_action("read_element", target=query, app=app_label, method="UI Automation", result="SUCCESS", elapsed=elapsed)
    return ActionResult(True, info.get("text") or info.get("name") or "(empty)", "uia", {"element": info})


def select_element(query: str, item: str = "", app: str = "", control_type: str = "") -> ActionResult:
    t0 = time.monotonic()
    query = _pick_query(query)
    win, winfo = _resolve_uia_window(app)
    app_label = winfo.process_name if winfo else app
    el = uia.find_element(win, query=query, control_type=control_type) if win else None
    elapsed = time.monotonic() - t0
    if el is None:
        log_action("select_element", target=query, app=app_label, method="UI Automation", result="FAILED", elapsed=elapsed)
        return ActionResult(False, f"Could not find '{query}' to select.")
    res = uia.select_element(el, item=item)
    log_action("select_element", target=query, app=app_label, method="UI Automation",
               result="SUCCESS" if res.ok else "FAILED", elapsed=elapsed)
    return ActionResult(res.ok, res.message, "uia")


def clear_element(query: str, app: str = "", control_type: str = "") -> ActionResult:
    t0 = time.monotonic()
    query = _pick_query(query)
    win, winfo = _resolve_uia_window(app)
    app_label = winfo.process_name if winfo else app
    el = uia.find_element(win, query=query, control_type=control_type) if win else None
    elapsed = time.monotonic() - t0
    if el is None:
        log_action("clear_element", target=query, app=app_label, method="UI Automation", result="FAILED", elapsed=elapsed)
        return ActionResult(False, f"Could not find '{query}' to clear.")
    res = uia.clear_element(el)
    log_action("clear_element", target=query, app=app_label, method="UI Automation",
               result="SUCCESS" if res.ok else "FAILED", elapsed=elapsed)
    return ActionResult(res.ok, res.message, "uia")


def get_ui_tree(app: str = "", max_depth: int = 3, name_filter: str = "", max_elements: int = 150) -> ActionResult:
    t0 = time.monotonic()
    win, winfo = _resolve_uia_window(app)
    tree = uia.get_ui_tree(win, max_depth=max_depth, name_filter=name_filter, max_elements=max_elements)
    elapsed = time.monotonic() - t0
    ok = "error" not in tree
    log_action("get_ui_tree", target=name_filter, app=winfo.process_name if winfo else app,
               method="UI Automation", result="SUCCESS" if ok else "FAILED", elapsed=elapsed)
    if not ok:
        return ActionResult(False, tree.get("error", "Could not read UI tree."))
    return ActionResult(True, "UI tree captured.", "uia", tree)


def wait_for_element(query: str, app: str = "", control_type: str = "", timeout: float = 8.0) -> ActionResult:
    t0 = time.monotonic()
    win, winfo = _resolve_uia_window(app)
    el = uia.wait_for_element(win, query=query, control_type=control_type, timeout=timeout) if win else None
    elapsed = time.monotonic() - t0
    app_label = winfo.process_name if winfo else app
    if el is None:
        log_action("wait_for_element", target=query, app=app_label, method="UI Automation", result="TIMEOUT", elapsed=elapsed)
        return ActionResult(False, f"'{query}' did not appear within {timeout:.0f}s.")
    ref = uia.element_to_ref(el, app=app_label, window_title=winfo.title if winfo else "")
    get_context().remember_elements([ref])
    log_action("wait_for_element", target=query, app=app_label, method="UI Automation", result="SUCCESS", elapsed=elapsed)
    return ActionResult(True, f"'{query}' appeared after {elapsed:.1f}s.", "uia", {"element": ref.__dict__})
