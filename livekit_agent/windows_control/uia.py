"""
Level 2 — UI Automation.

The primary way JARVIS interacts with the *inside* of an application window:
finding buttons/fields/menus/lists by name, automation id, control type or
class name, reading their text/state, clicking, selecting, typing into them,
and walking a bounded slice of the accessibility tree.

Built on pywinauto's "uia" backend (already a project dependency), talking
directly to Microsoft UI Automation — no mouse/keyboard emulation involved
at this level, so it keeps working even if the window is partially covered
or off-screen.

Not every application exposes a rich UIA tree (some Electron/Qt apps only
partially implement accessibility) — when find_element comes up empty, the
router (router.py) falls back to keyboard/vision automatically. That's by
design, not a bug in this layer.
"""
from __future__ import annotations

import platform
import time
from dataclasses import dataclass

from . import native
from .context import ElementRef

_IS_WINDOWS = platform.system() == "Windows"

if _IS_WINDOWS:
    from pywinauto import Desktop
    from pywinauto.base_wrapper import BaseWrapper
else:  # pragma: no cover
    Desktop = None
    BaseWrapper = object

# Control types the spec asks us to support explicitly. UIA will happily
# return others too (Group, Image, ...) — this is just what we actively
# reason about / expose in get_ui_tree summaries.
SUPPORTED_CONTROL_TYPES = {
    "Button", "Edit", "Text", "CheckBox", "RadioButton", "ComboBox",
    "List", "ListItem", "Menu", "MenuItem", "Tab", "TabItem",
    "Tree", "TreeItem", "Window", "Pane", "Hyperlink",
}


def _require_windows():
    if not _IS_WINDOWS:
        raise RuntimeError("windows_control.uia requires Windows.")


@dataclass
class UIAResult:
    ok: bool
    message: str
    element: "object | None" = None
    ref: ElementRef | None = None


def get_desktop():
    _require_windows()
    return Desktop(backend="uia")


def prewarm() -> None:
    """
    Pays UI Automation's one-time COM/IUIAutomation initialization cost
    (measured: ~6ms on the very first call in a process, ~0.6ms on every
    call after — pywinauto keeps the interface pointer alive internally,
    so there is nothing to cache ourselves beyond just having made one call)
    up front, during startup, instead of during the user's first spoken
    command that happens to touch window_manager/ui_automation.
    Safe to call from a background thread; swallows all errors — a failed
    prewarm just means the first real call pays the cost instead, same as
    if this function didn't exist.
    """
    if not _IS_WINDOWS:
        return
    try:
        active = native.get_active_window()
        if active:
            get_desktop().window(handle=active.hwnd).wrapper_object()
    except Exception:
        pass


def resolve_window(app_or_title: str = "", hwnd: int | None = None):
    """Return a pywinauto top-level window wrapper, or None."""
    _require_windows()
    target_hwnd = hwnd
    if target_hwnd is None and app_or_title:
        match = native.find_windows(query=app_or_title)
        if match:
            target_hwnd = match[0].hwnd
    if target_hwnd is None:
        active = native.get_active_window()
        target_hwnd = active.hwnd if active else None
    if target_hwnd is None:
        return None
    try:
        win = get_desktop().window(handle=target_hwnd)
        win.wrapper_object()  # forces resolution / raises if gone
        return win
    except Exception:
        return None


def _element_info(el: BaseWrapper) -> dict:
    ei = el.element_info
    try:
        rect = ei.rectangle
        rect = (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:
        rect = None
    return {
        "name": ei.name or "",
        "control_type": ei.control_type or "",
        "automation_id": ei.automation_id or "",
        "class_name": ei.class_name or "",
        "rect": rect,
        "enabled": bool(getattr(ei, "enabled", True)),
        "visible": bool(getattr(ei, "visible", True)),
    }


def _matches(info: dict, query: str, control_type: str = "") -> bool:
    if control_type and info["control_type"].lower() != control_type.lower():
        return False
    if not query:
        return True
    q = query.strip().lower()
    return (
        q in info["name"].lower()
        or q == info["automation_id"].lower()
        or q in info["automation_id"].lower()
        or q in info["class_name"].lower()
    )


def find_elements(window, query: str = "", control_type: str = "",
                   max_depth: int = 8, limit: int = 25) -> list[BaseWrapper]:
    """Search descendants of `window` for elements matching query/control_type.
    Exact name/automation_id matches are ranked before partial substring ones."""
    _require_windows()
    if window is None:
        return []
    try:
        kwargs = {"depth": max_depth}
        if control_type:
            kwargs["control_type"] = control_type
        descendants = window.descendants(**kwargs)
    except Exception:
        return []

    exact, partial = [], []
    q = query.strip().lower() if query else ""
    for el in descendants:
        try:
            info = _element_info(el)
        except Exception:
            continue
        if not _matches(info, query, control_type):
            continue
        if q and (info["name"].lower() == q or info["automation_id"].lower() == q):
            exact.append(el)
        else:
            partial.append(el)
        if len(exact) + len(partial) >= limit * 3:
            break
    ordered = exact + partial
    return ordered[:limit]


def find_element(window, query: str = "", control_type: str = "",
                  index: int = 0, max_depth: int = 8) -> BaseWrapper | None:
    matches = find_elements(window, query=query, control_type=control_type, max_depth=max_depth)
    if not matches:
        return None
    if index < 0 or index >= len(matches):
        index = 0
    return matches[index]


def element_to_ref(el: BaseWrapper, app: str = "", window_title: str = "") -> ElementRef:
    info = _element_info(el)
    return ElementRef(
        name=info["name"], control_type=info["control_type"],
        automation_id=info["automation_id"], class_name=info["class_name"],
        rect=info["rect"], app=app, window_title=window_title, found_via="uia",
    )


def click_element(el: BaseWrapper, double: bool = False, button: str = "left") -> UIAResult:
    _require_windows()
    try:
        el.set_focus()
    except Exception:
        pass
    try:
        if double:
            el.double_click_input()
        elif button == "right":
            el.right_click_input()
        else:
            el.click_input()
        return UIAResult(True, "Clicked via UI Automation.", element=el)
    except Exception as e:
        # Some controls (menu items, list items) respond to Invoke pattern
        # even when synthetic mouse coordinates fail (off-screen, occluded).
        try:
            el.invoke()
            return UIAResult(True, "Invoked via UI Automation.", element=el)
        except Exception:
            return UIAResult(False, f"UI Automation click failed: {e}")


def type_into_element(el: BaseWrapper, text: str, clear_first: bool = True) -> UIAResult:
    _require_windows()
    try:
        el.set_focus()
    except Exception:
        pass
    # Edit-style controls expose set_edit_text — fastest & most reliable.
    if hasattr(el, "set_edit_text"):
        try:
            if clear_first:
                el.set_edit_text(text)
            else:
                current = ""
                try:
                    current = el.window_text() or ""
                except Exception:
                    pass
                el.set_edit_text(current + text)
            return UIAResult(True, "Typed via UI Automation (set_edit_text).", element=el)
        except Exception:
            pass
    # Generic fallback: real synthetic keystrokes into the focused control.
    try:
        if clear_first:
            el.type_keys("^a{DELETE}", set_foreground=True)
        el.type_keys(text, with_spaces=True, with_tabs=True, set_foreground=True)
        return UIAResult(True, "Typed via UI Automation (type_keys).", element=el)
    except Exception as e:
        return UIAResult(False, f"UI Automation type failed: {e}")


def read_element(el: BaseWrapper) -> dict:
    info = _element_info(el)
    text = ""
    try:
        text = el.window_text() or ""
    except Exception:
        pass
    try:
        if not text and hasattr(el, "texts"):
            texts = el.texts()
            text = " ".join(t for t in texts if t) if texts else ""
    except Exception:
        pass
    info["text"] = text
    try:
        info["is_enabled"] = bool(el.is_enabled())
        info["is_visible"] = bool(el.is_visible())
    except Exception:
        pass
    return info


def select_element(el: BaseWrapper, item: str = "") -> UIAResult:
    """Select an item in a list/combo/menu — or select the element itself
    if it's already the leaf item (e.g. a ListItem/TabItem/MenuItem)."""
    _require_windows()
    try:
        if item and hasattr(el, "select"):
            el.select(item)
            return UIAResult(True, f"Selected '{item}'.", element=el)
        if hasattr(el, "select"):
            el.select()
            return UIAResult(True, "Selected.", element=el)
        return click_element(el)
    except Exception as e:
        return UIAResult(False, f"Select failed: {e}")


def clear_element(el: BaseWrapper) -> UIAResult:
    _require_windows()
    try:
        if hasattr(el, "set_edit_text"):
            el.set_edit_text("")
        else:
            el.set_focus()
            el.type_keys("^a{DELETE}", set_foreground=True)
        return UIAResult(True, "Field cleared.", element=el)
    except Exception as e:
        return UIAResult(False, f"Clear failed: {e}")


def get_children(el: BaseWrapper) -> list[dict]:
    try:
        return [_element_info(c) for c in el.children()]
    except Exception:
        return []


def get_parent(el: BaseWrapper) -> dict | None:
    try:
        p = el.parent()
        return _element_info(p) if p is not None else None
    except Exception:
        return None


def get_ui_tree(window, max_depth: int = 3, name_filter: str = "",
                 max_elements: int = 150) -> dict:
    """
    Bounded, filtered accessibility tree — small enough to hand to an LLM.
    Only depth/count-limited, optionally filtered by a name substring so a
    caller looking for "the send button" doesn't get the whole DOM back.
    """
    if window is None:
        return {"error": "window not found"}

    root_info = {}
    try:
        root_info = _element_info(window.wrapper_object())
    except Exception:
        pass

    count = [0]
    name_q = name_filter.strip().lower() if name_filter else ""

    def _walk(el, depth) -> dict | None:
        if count[0] >= max_elements:
            return None
        try:
            info = _element_info(el)
        except Exception:
            return None
        count[0] += 1
        node = {
            "name": info["name"], "control_type": info["control_type"],
            "automation_id": info["automation_id"],
        }
        if depth < max_depth and count[0] < max_elements:
            children = []
            try:
                for c in el.children():
                    if count[0] >= max_elements:
                        break
                    child_node = _walk(c, depth + 1)
                    if child_node:
                        children.append(child_node)
            except Exception:
                pass
            if children:
                node["children"] = children
        return node

    try:
        root_wrapper = window.wrapper_object()
    except Exception as e:
        return {"error": str(e)}

    if name_q:
        matches = find_elements(window, query=name_filter, max_depth=max_depth, limit=max_elements)
        return {
            "root": root_info.get("name", ""),
            "matches": [_element_info(m) for m in matches],
        }

    tree = _walk(root_wrapper, 0)
    return {"root": root_info.get("name", ""), "tree": tree, "element_count": count[0]}


def wait_for_element(window, query: str = "", control_type: str = "",
                      timeout: float = 8.0, poll: float = 0.4) -> BaseWrapper | None:
    deadline = time.monotonic() + timeout
    while True:
        el = find_element(window, query=query, control_type=control_type)
        if el is not None:
            return el
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)
