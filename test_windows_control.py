"""
Integration test for the Windows Control Layer (windows_control/ +
actions/window_control.py). Not a unit-test-with-mocks suite — this drives
REAL Windows apps (Notepad, Calculator) exactly the way JARVIS's LLM tool
calls would, and checks the actual on-screen/process result, per the
project's own "verify, don't assume" requirement.

Run directly:  python test_windows_control.py
Exit code 0 = all required checks passed. Optional/best-effort checks (some
apps have weak UI Automation support by design — that's what the vision
fallback exists for) are reported but don't fail the run.

No new test-framework dependency was added: the project has no existing
test suite or pytest install, so this stays a plain, dependency-free script
consistent with everything else in actions/*.py.
"""
from __future__ import annotations

import platform
import sys
import time

# Same UTF-8 stdout/stderr fix main.py applies at startup — needed here too
# since this script (unlike main.py) can be launched directly from a
# non-UTF8 console codepage, and several actions/*.py modules print emoji /
# Unicode arrows unconditionally.
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

if platform.system() != "Windows":
    print("SKIP: this test suite only runs on Windows.")
    sys.exit(0)

import psutil

from windows_control import native
from windows_control.context import get_context, reset_context
from actions.window_control import window_manager, ui_automation, launch_and_verify

PASS, FAIL, SKIP = [], [], []


def check(name: str, condition: bool, detail: str = "", required: bool = True):
    status = "PASS" if condition else ("FAIL" if required else "SKIP")
    bucket = PASS if condition else (FAIL if required else SKIP)
    bucket.append(name)
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    return condition


def cleanup_process(name_stem: str):
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if name_stem.lower() in (p.info.get("name") or "").lower():
                p.kill()
        except Exception:
            pass


def section(title: str):
    print(f"\n=== {title} ===")


# ── 0. cleanup from any previous failed run ─────────────────────────────
cleanup_process("notepad.exe")
time.sleep(0.3)
reset_context()

# ── 1. native layer: process/window enumeration ─────────────────────────
section("Level 1 — native.py")
windows = native.list_windows()
check("list_windows returns a list", isinstance(windows, list) and len(windows) > 0,
      f"{len(windows)} windows")

procs = native.list_processes()
check("list_processes returns a list", isinstance(procs, list) and len(procs) > 0,
      f"{len(procs)} processes")

active = native.get_active_window()
check("get_active_window returns something", active is not None,
      active.title if active else "")

# ── 2. smart launch: launch_app / open_app verification wrapper ─────────
section("Level 1+2 — smart launch (launch_and_verify / open_app tool)")
msg = launch_and_verify("Notepad")
check("launch_and_verify launched Notepad", "notepad" in msg.lower() or "opened" in msg.lower(), msg)

notepad_win = native.wait_for_window(query="notepad", timeout=8.0)
check("Notepad window appeared", notepad_win is not None,
      notepad_win.title if notepad_win else "not found", required=True)

if notepad_win is None:
    print("\nFATAL: cannot continue without Notepad — aborting remaining tests.")
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed, {len(SKIP)} skipped/optional.")
    sys.exit(1)

# already-running path
msg2 = launch_and_verify("Notepad")
check("second launch_and_verify recognizes it's already running",
      "already running" in msg2.lower(), msg2)

ctx = get_context()
check("ExecutionContext recorded the active app", ctx.active_app.lower() == "notepad" or ctx.active_hwnd == notepad_win.hwnd,
      f"active_app={ctx.active_app!r} hwnd={ctx.active_hwnd}")

# ── 3. window_manager tool: minimize/restore/maximize/move/resize/focus ─
section("window_manager tool")

r = window_manager(parameters={"action": "list_windows", "app": "notepad"})
check("window_manager list_windows finds Notepad", "notepad" in r.lower(), r[:120])

r = window_manager(parameters={"action": "minimize", "app": "notepad"})
time.sleep(0.4)
w = native.find_windows(query="notepad")[0]
check("window_manager minimize actually minimized", w.is_minimized, r)

r = window_manager(parameters={"action": "restore", "app": "notepad"})
time.sleep(0.4)
w = native.find_windows(query="notepad")[0]
check("window_manager restore un-minimized", not w.is_minimized, r)

r = window_manager(parameters={"action": "maximize", "app": "notepad"})
time.sleep(0.4)
w = native.find_windows(query="notepad")[0]
check("window_manager maximize actually maximized", w.is_maximized, r)

r = window_manager(parameters={"action": "restore", "app": "notepad"})
time.sleep(0.3)

r = window_manager(parameters={"action": "move", "app": "notepad", "x": 60, "y": 60})
time.sleep(0.3)
w = native.find_windows(query="notepad")[0]
check("window_manager move repositioned the window", w.rect[0] in range(40, 100), f"rect={w.rect}")

r = window_manager(parameters={"action": "resize", "app": "notepad", "width": 700, "height": 500})
time.sleep(0.3)
w = native.find_windows(query="notepad")[0]
resized_w = w.rect[2] - w.rect[0]
check("window_manager resize changed window size", 650 <= resized_w <= 760, f"width={resized_w}")

r = window_manager(parameters={"action": "focus", "app": "notepad"})
time.sleep(0.2)
active = native.get_active_window()
check("window_manager focus brought Notepad to foreground",
      active is not None and "notepad" in (active.process_name or "").lower(),
      active.title if active else "")

r = window_manager(parameters={"action": "get_active"})
check("window_manager get_active reports Notepad", "notepad" in r.lower(), r)

r = window_manager(parameters={"action": "list_processes", "app": "notepad"})
check("window_manager list_processes finds notepad.exe", "notepad" in r.lower(), r[:120])

# ── 4. ui_automation tool: find / click / type / read / get_tree ────────
section("ui_automation tool")

r = ui_automation(parameters={"action": "get_tree", "app": "notepad", "max_depth": 4})
check("ui_automation get_tree returns structured data", r.startswith("{") and "root" in r, r[:150])

# Note: query="" + control_type="Document" is a pure control-type search —
# Notepad's text area doesn't expose "Document" as its accessible NAME (that
# would be the localized window title/description), so combining both as a
# name filter would never match. This is how a real ui_automation caller
# should scope by role instead of guessing an exact name.
r = ui_automation(parameters={"action": "find", "app": "notepad", "query": "", "control_type": "Document"})
found_doc = "found" in r.lower()
check("ui_automation find locates the text area", found_doc, r, required=False)

r = ui_automation(parameters={
    "action": "type", "app": "notepad", "query": "",
    "control_type": "Document", "text": "Hello from JARVIS windows_control test",
})
typed_ok = "typed" in r.lower()
check("ui_automation type wrote into the document", typed_ok, r)

r = ui_automation(parameters={"action": "read", "app": "notepad", "query": "", "control_type": "Document"})
check("ui_automation read gets the typed text back",
      "hello from jarvis" in r.lower(), r[:150])

r = ui_automation(parameters={"action": "find", "app": "notepad", "query": "___NoSuchElementXYZ___"})
check("ui_automation find fails gracefully (no crash) for a missing element",
      "could not find" in r.lower() or "found" not in r.lower(), r)

# ── 5. error recovery: nonexistent app doesn't crash any tool ──────────
section("Error recovery")

r = window_manager(parameters={"action": "focus", "app": "ThisAppDoesNotExist12345"})
check("window_manager focus on missing app fails gracefully", "no window found" in r.lower(), r)

r = ui_automation(parameters={"action": "click", "app": "ThisAppDoesNotExist12345", "query": "Nothing"})
check("ui_automation click on missing app fails gracefully (no exception)", isinstance(r, str) and len(r) > 0, r)

# protected process guard
explorer = native.list_processes(name_filter="explorer.exe")
if explorer:
    ok, kmsg = native.kill_process(explorer[0].pid, force=False)
    check("native.kill_process refuses to kill explorer.exe", ok is False, kmsg)

# ── 6. action chain: launch -> wait -> focus -> type -> verify -> close ─
section("Action chain (multi-step)")

chain_ok = True
steps = []
try:
    m1 = launch_and_verify("Notepad")
    steps.append(("launch", "notepad" in m1.lower()))
    m2 = window_manager(parameters={"action": "focus", "app": "notepad"})
    steps.append(("focus", True))
    m3 = ui_automation(parameters={"action": "type", "app": "notepad", "query": "",
                                    "control_type": "Document", "text": " CHAIN_OK", "clear_first": "false"})
    steps.append(("type", "typed" in m3.lower()))
    m4 = ui_automation(parameters={"action": "read", "app": "notepad", "query": "", "control_type": "Document"})
    steps.append(("verify", "chain_ok" in m4.lower()))
except Exception as e:
    steps.append(("exception", False))
    print(f"Chain exception: {e}")

chain_ok = all(ok for _, ok in steps)
check("full action chain (launch->focus->type->verify) succeeded", chain_ok, str(steps))

# ── 7. window_manager close (graceful) ───────────────────────────────────
section("Close")

# Force-kill instead of graceful close for the test run — Notepad will
# otherwise show a blocking "Save changes?" dialog for the typed text.
cleanup_process("notepad.exe")
time.sleep(0.3)
still_there = native.find_windows(query="notepad")
check("cleanup: no leftover Notepad window", len(still_there) == 0, str(still_there))

# ── summary ───────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed, {len(SKIP)} skipped/optional")
if FAIL:
    print("FAILED:")
    for f in FAIL:
        print(f"  - {f}")
sys.exit(1 if FAIL else 0)
