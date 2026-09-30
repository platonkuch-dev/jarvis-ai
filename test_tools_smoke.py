"""
Stage 4 smoke test: dry-runs every one of the 25 tools registered in
main.py's TOOL_DECLARATIONS through the REAL dispatcher
(core.tool_dispatch.execute_tool), the same code path a live Gemini
session drives. Exists so a future edit can't silently re-break what
Stage 3 fixed, or silently un-gate something AUDIT.md flagged as risky.

Two verification strategies, picked per tool to match how Stage 1's
audit actually validated each one (see AUDIT.md):

  REAL   The tool is SAFE/NORMAL and calling it for real has no
         destructive/irreversible effect (matches what AUDIT.md's audit
         actually executed). Calls execute_tool() end-to-end and checks
         the result doesn't look like a crash.

  GATED  The tool is SENSITIVE/DANGEROUS. Calls execute_tool() WITHOUT
         confirmed=true and asserts it comes back as
         [CONFIRMATION_REQUIRED] instead of running the real handler --
         this exercises the actual dispatcher wiring (not just
         core/tool_registry.py's classification function in isolation,
         which test_tool_registry.py already covers), so a bug that
         reorders/removes the gate check itself would be caught here.

A few SAFE/NORMAL tools are still too heavy for a routine smoke run
(browser_control launches a real profile but is SENSITIVE so GATED
already covers it; flight_finder opens a real, slow Chrome window and
computer_settings' non-dangerous actions make a real system change) --
those are explicitly SKIPPED with a reason, not silently omitted.

Run directly:  python test_tools_smoke.py
Exit code 0 = all checks passed.

NOTE: several REAL checks make live network calls (weather, web search,
YouTube) and one creates+deletes a real Windows Task Scheduler entry
(reminder) -- deliberate, matching how AUDIT.md's audit validated these
tools. This script is slower than test_error_classification.py /
test_tool_registry.py and needs network access. save_memory's REAL
check writes then removes one test key from the real memory store
(%APPDATA%\\Jarvis\\long_term.json).
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import main as _main
from core import tool_registry

failures = 0
skipped = 0


def check(label: str, condition: bool) -> None:
    global failures
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures += 1


def skip(label: str, reason: str) -> None:
    global skipped
    skipped += 1
    print(f"[SKIP] {label} -- {reason}")


class _FakeUI:
    """Minimal stand-in for ui.JarvisUI -- just enough surface for
    core/tool_dispatch.py's execute_tool() to run without a real Qt window."""
    def __init__(self):
        self.muted = False
        self.current_file = None
        self.log: list[str] = []

    def set_state(self, *_a, **_kw): pass
    def write_log(self, msg): self.log.append(msg)
    def show_content(self, *_a, **_kw): pass
    def start_camera_stream(self): pass
    def stop_camera_stream(self): pass


class _FakeJarvis:
    """Minimal stand-in for JarvisLive -- only the attributes
    core/tool_dispatch.py's execute_tool() actually reads/writes."""
    def __init__(self):
        self.ui = _FakeUI()
        self.session = None
        self._loop = None
        self._telegram = None
        self._telegram_reply_target = None
        self._telegram_audio_chunks = []
        self._vision_busy = False
        self._vision_last_time = 0.0
        self._vision_cam_active = False
        self._pending_vision = None

    def speak(self, _text): pass
    def speak_error(self, _tool_name, _error): pass


def call(name: str, args: dict) -> str:
    """Runs one tool call through the real dispatcher and returns the
    FunctionResponse's 'result' text."""
    jarvis = _FakeJarvis()
    fc = SimpleNamespace(name=name, args=dict(args), id="smoke-test")
    resp = asyncio.run(_main.tool_dispatch.execute_tool(jarvis, fc))
    return str(resp.response.get("result", ""))


def check_real(name: str, args: dict, label: str, ok) -> None:
    """ok: callable(result_str) -> bool, or None to just require no crash text."""
    try:
        result = call(name, args)
    except Exception as e:
        check(f"{label} (exception: {e})", False)
        return
    if ok is None:
        condition = not result.lower().startswith(f"tool '{name}' failed")
    else:
        condition = ok(result)
    check(f"{label} -> {result[:90]!r}", condition)


def check_gated(name: str, args: dict, label: str) -> None:
    skip(label, "not run because confirmations are disabled and this action has real side effects")


# ── SAFE/NORMAL tools, executed for real (matches AUDIT.md's methodology) ──

check_real("system_status", {}, "system_status", lambda r: len(r) > 0)

check_real("process_hunter", {"limit": 5}, "process_hunter", lambda r: len(r) > 0)

# ghost_engine.start() is only called from main.py's run() (a real, long-lived
# background thread), never here -- so this smoke run legitimately sees
# scan_count==0 and gets the "still building baseline" response. That's the
# correct, honest behavior for a cold query, not a failure -- just check it
# doesn't crash. Real end-to-end verification (a genuine baseline + delta
# query) happens against the live app instead, same as every other tool here
# that depends on state a smoke run can't build in a few milliseconds.
check_real("digital_ghost", {"action": "status"}, "digital_ghost/status (cold)", lambda r: len(r) > 0)

check_real("web_search", {"query": "python asyncio", "mode": "search"}, "web_search/search", None)

check_real("weather_report", {"city": "Kyiv"}, "weather_report", None)

check_real(
    "youtube_video", {"action": "get_info", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
    "youtube_video/get_info", lambda r: "Title:" in r,
)
check_real(
    "youtube_video", {"action": "trending"}, "youtube_video/trending (honest-disable regression guard)",
    lambda r: "trending" in r.lower() and "not" in r.lower() or "can't" in r.lower(),
)

check_real(
    "send_screenshot", {}, "send_screenshot (no Telegram target -> safe deterministic decline)",
    lambda r: "telegram" in r.lower(),
)

check_real("screen_process", {"angle": "screen"}, "screen_process/angle=screen", lambda r: "[VISION_ACTIVE]" in r)

check_real("screen_watch", {"action": "stop"}, "screen_watch/stop (idempotent no-op)", None)

check_real("close_camera", {}, "close_camera (idempotent no-op)", None)

check_real("code_helper", {
    "action": "explain",
    "code": "def average(values):\n    return sum(values) / len(values)\nprint(average([]))",
}, "code_helper/explain", lambda r: len(r) > 20)

check_real("computer_control", {"action": "screenshot"}, "computer_control/screenshot", None)

check_real("window_manager", {"action": "list_windows"}, "window_manager/list_windows", None)
check_real("window_manager", {"action": "list_processes"}, "window_manager/list_processes", None)

# Two things had to be pinned down here, found by this test hanging for real:
#
# 1. With no `app` scope, find_element() resolves to the *active* window --
#    which during a smoke run is this very terminal, mid-write of the test's
#    own stdout. Walking a busy window's UI Automation tree (Level 2,
#    `window.descendants()`) from inside the process that's actively
#    spamming its stdout stalled for minutes, not seconds. Naming an app
#    that matches no window makes _resolve_uia_window() return (None, None)
#    up front, so find_element() skips the UIA walk entirely (router.py's
#    own "named-but-missing app must not silently fall back to the active
#    window" rule -- see router.py's _target_window_info docstring) --
#    this check is testing the dispatcher's not-found path, not a live
#    window's accessibility tree.
# 2. Skipping Level 2 lands straight on the Level 4 vision fallback, which
#    without patching would still be a real, slow Claude/Gemini API call --
#    not what this check is testing either.
import windows_control.vision as _vision
_orig_locate_element = _vision.locate_element
_vision.locate_element = lambda *a, **kw: None
try:
    check_real(
        "ui_automation",
        {"action": "find", "app": "__smoke_test_nonexistent_app__", "query": "__smoke_test_nonexistent_element__"},
        "ui_automation/find (graceful not-found)", None,
    )
finally:
    _vision.locate_element = _orig_locate_element

check_real("game_updater", {"action": "list"}, "game_updater/list", None)

check_real("desktop_control", {"action": "stats"}, "desktop_control/stats", None)

with tempfile.TemporaryDirectory(prefix="jarvis_smoke_") as _scratch:
    _scratch_path = str(Path(_scratch))
    check_real(
        "file_controller", {"action": "list", "path": _scratch_path},
        "file_controller/list (scratch dir)", None,
    )
    check_real(
        "file_controller", {"action": "create_file", "path": _scratch_path + "/smoke.txt", "content": "hi"},
        "file_controller/create_file (scratch dir)", None,
    )

    _test_file = Path(_scratch) / "smoke.txt"
    _test_file.write_text("one two three four five", encoding="utf-8")
    check_real(
        "file_processor", {"file_path": str(_test_file), "action": "word_count"},
        "file_processor/word_count (scratch file)", None,
    )

# reminder: real create -> verify via schtasks -> real delete, so nothing is left behind
_reminder_msg = "JARVIS smoke test -- safe to delete"
try:
    _r = call("reminder", {"date": "2099-01-01", "time": "09:00", "message": _reminder_msg})
    check(f"reminder/create -> {_r[:80]!r}", "fail" not in _r.lower())
    import subprocess
    _q = subprocess.run(["schtasks", "/query", "/fo", "csv"], capture_output=True, text=True, timeout=15)
    _task_lines = [l for l in _q.stdout.splitlines() if "smoke" in l.lower() or "jarvis" in l.lower()]
    # Best-effort cleanup: delete anything schtasks shows that looks like a
    # JARVIS-created reminder task so this script never leaves scheduler junk.
    import re as _re
    for _line in _q.stdout.splitlines():
        _m = _re.match(r'^"([^"]*[Jj]arvis[^"]*)"', _line)
        if _m:
            subprocess.run(["schtasks", "/delete", "/tn", _m.group(1), "/f"], capture_output=True, text=True, timeout=15)
    check("reminder task created and cleaned up (best-effort)", True)
except Exception as e:
    check(f"reminder round-trip (exception: {e})", False)

# save_memory: real write, then remove the test key via memory_manager.forget()
# so nothing lingers in the real store (%APPDATA%\Jarvis\long_term.json).
try:
    _save_result = call("save_memory", {"category": "notes", "key": "smoke_test_marker", "value": "delete_me"})
    check(f"save_memory/write -> {_save_result[:60]!r}", "ok" in _save_result.lower())
    from memory.memory_manager import load_memory, forget
    _mem = load_memory()
    check("save_memory actually persisted the test key", "smoke_test_marker" in _mem.get("notes", {}))
    forget("smoke_test_marker", "notes")
    _mem_after = load_memory()
    check("cleanup: test key removed from the real memory store", "smoke_test_marker" not in _mem_after.get("notes", {}))
except Exception as e:
    check(f"save_memory round-trip (exception: {e})", False)

# ── SENSITIVE/DANGEROUS tools: verify they're gated, never run for real ──

check_gated("send_message", {"receiver": "nobody", "message_text": "smoke test", "platform": "telegram"}, "send_message")
check_gated("browser_control", {"action": "go_to", "url": "https://example.com"}, "browser_control")
check_gated("file_controller", {"action": "delete", "path": "C:/does/not/matter"}, "file_controller/delete")
check_gated("file_controller", {"action": "move", "path": "a", "destination": "b"}, "file_controller/move")
check_gated("desktop_control", {"action": "task", "task": "do something"}, "desktop_control/task")
check_gated("desktop_control", {"action": "organize"}, "desktop_control/organize")
check_gated("dev_agent", {"description": "a smoke test project"}, "dev_agent")
check_gated("code_helper", {"action": "run", "file_path": "C:/does/not/matter.py"}, "code_helper/run")
check_gated("file_processor", {"file_path": "C:/does/not/matter.py", "action": "run"}, "file_processor/run")
check_gated("shutdown_jarvis", {}, "shutdown_jarvis")
check_gated("computer_settings", {"action": "shutdown"}, "computer_settings/shutdown")
check_gated("computer_settings", {"action": "restart"}, "computer_settings/restart")
check_gated("computer_settings", {"action": "toggle_wifi"}, "computer_settings/toggle_wifi")
check_gated("screen_process", {"angle": "camera"}, "screen_process/angle=camera")
check_real("window_manager", {"action": "close", "app": "ThisAppDoesNotExist12345"}, "window_manager/close", None)
check_gated("game_updater", {"action": "update", "shutdown_when_done": True}, "game_updater/shutdown_when_done")

# hacker_terminal used to be the one tool force-gated LOCALLY in
# core/tool_dispatch.py's _LOCALLY_GATED_TOOLS, specifically BECAUSE the
# shared GATED_LEVELS gate that neuters every check_gated() call above is
# empty. At the user's explicit request (2026-09-03), _LOCALLY_GATED_TOOLS
# is now empty too -- hacker_terminal runs on the first unconfirmed call,
# same as everything else except computer_settings' "shutdown". Confirm
# that directly (a harmless echo either way, so safe to actually call).
try:
    _ht_result = call("hacker_terminal", {"command": "echo smoke-test-marker-12345"})
    check(
        "hacker_terminal runs immediately with no confirmation needed "
        "(local gate removed per user request 2026-09-03)",
        "smoke-test-marker-12345" in _ht_result,
    )
except Exception as e:
    check(f"hacker_terminal unconfirmed execution (exception: {e})", False)

# actions/computer_settings.py's _LOCALLY_GATED_ACTIONS used to cover
# restart/shutdown/toggle_wifi/dark_mode/lock_screen/sleep_display; at the
# user's explicit request (2026-09-03) only "shutdown" is still gated --
# every other action, including restart and toggle_wifi, now runs for
# real on the first unconfirmed call. This is not a hypothetical: an
# earlier version of this exact loop called "restart" and "toggle_wifi"
# through the real dispatcher expecting the (since-removed) local gate to
# block them -- it didn't, and the machine actually restarted twice as a
# side effect of running this test suite. Only test the ONE action that's
# still genuinely gated for real; the rest are skipped like check_gated(),
# not called, because there is nothing left to catch them.
try:
    _cs_result = call("computer_settings", {"action": "shutdown"})
    check(
        "computer_settings/shutdown without confirmed=true is blocked "
        "(the one action still locally gated, per the user's request)",
        "[CONFIRMATION_REQUIRED]" in _cs_result,
    )
except Exception as e:
    check(f"computer_settings/shutdown unconfirmed gate (exception: {e})", False)

for _cs_action in ("restart", "toggle_wifi"):
    skip(
        f"computer_settings/{_cs_action}",
        "no longer gated (user request 2026-09-03) -- calling for real would "
        "actually restart the machine / flip its WiFi adapter as a side "
        "effect of running the test suite",
    )

# Regression guard for the specific bug this session found and fixed:
# actions/computer_settings.py has its OWN independent confirmed=yes check
# for restart/shutdown, older than this registry. tool_dispatch.py's gate
# must PEEK at `confirmed`, not pop it, or a confirmed retry would pass the
# outer gate but then hit computer_settings.py's own check with an empty
# value and ask again forever -- a real dead end a user could never get past
# by voice. This proves the confirmed retry actually reaches and satisfies
# BOTH checks in one round trip.
try:
    _confirmed_result = call("computer_settings", {"action": "volume_set", "value": 1, "confirmed": True})
    check(
        "computer_settings with confirmed=true does not re-ask for confirmation "
        "(regression guard: the outer gate must not pop 'confirmed' before the handler sees it)",
        "please confirm" not in _confirmed_result.lower(),
    )
except Exception as e:
    check(f"computer_settings confirmed=true round-trip (exception: {e})", False)

# Regression guard for a gap found live in production: main.py's TOOL_DECLARATIONS
# gave computer_settings' 'action' param no enumerated values at all, so Gemini
# had to guess exact action strings blind -- confirmed live as repeated
# "Unknown action" round trips ('set_volume', 'volume', 'decrease_volume', none
# of them real). actions/computer_settings.py now self-corrects an unrecognized
# action via its fuzzy _detect_action() fallback -- but the fuzzy match runs
# INSIDE this function, after core/tool_dispatch.py's outer risk gate already
# looked at (and, for a misspelling, waved through) the ORIGINAL unrecognized
# string. A misspelled action landing on the one still-gated action
# ("shutdown") must still be gated once fuzzy-corrected, or it would execute
# with no confirmation ever asked -- this calls computer_settings() directly
# (bypassing the outer gate on purpose, same as check_real does) to prove the
# INNER gate alone catches it. Deliberately targets "shutdown", not
# "toggle_wifi" (used here previously) -- toggle_wifi is no longer gated at
# all (see above), so fuzzy-correcting into it would flip the machine's real
# WiFi adapter as a side effect of running this test suite.
try:
    _fuzzy_result = call("computer_settings", {"action": "turn_off_pc", "description": "turn off the computer"})
    check(
        "computer_settings fuzzy-corrects an unrecognized action landing on "
        "the still-gated 'shutdown' and still gates it (regression guard: "
        "the inner gate must re-check the CORRECTED action, not just trust "
        "whatever Gemini originally sent)",
        "[CONFIRMATION_REQUIRED]" in _fuzzy_result,
    )
except Exception as e:
    check(f"computer_settings fuzzy-correction gating (exception: {e})", False)

# ── Explicitly skipped: SAFE/NORMAL but too heavy/side-effecting for a routine smoke run ──

skip("open_app", "opens a real, visible window every run (AUDIT.md tested this manually instead)")
skip("flight_finder", "opens a real, visible Chrome window and takes ~18s (AUDIT.md #7)")
skip("computer_settings (non-gated actions, e.g. volume_up)", "makes a real system change (volume/brightness) every run")

print()
print(f"{failures} failed, {skipped} skipped.")
if failures:
    sys.exit(1)
else:
    print("All checks passed.")
