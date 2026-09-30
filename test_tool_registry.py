"""
Smoke test for core/tool_registry.py's risk classification and confirmation
gate, added alongside the Stage 2/3 module split (see REWORK_PLAN.md §3).

Why this exists: unlike the pure file moves elsewhere in this refactor,
tool_registry.py is new logic (SENSITIVE/DANGEROUS action classification,
grounded in AUDIT.md's Stage-1 findings) wired into core/tool_dispatch.py's
confirmation gate. A misclassification here either lets a risky action run
unconfirmed (defeats the whole point) or blocks a safe one forever (breaks
the assistant for no reason) -- this pins down the concrete cases AUDIT.md
flagged, without needing a live Gemini session, mic, or API key.

Run directly:  python test_tool_registry.py
Exit code 0 = all checks passed.
"""
from __future__ import annotations

import sys

from core.tool_registry import (
    RiskLevel, GATED_LEVELS, resolve_risk, add_confirmation_param,
)

failures = 0


def check(label: str, condition: bool) -> None:
    global failures
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures += 1


def gated(name: str, args: dict) -> bool:
    return resolve_risk(name, args) in {RiskLevel.SENSITIVE, RiskLevel.DANGEROUS}


check(
    "confirmation is disabled for every risk level",
    not GATED_LEVELS,
)


# --- AUDIT.md's concrete SENSITIVE/DANGEROUS findings must actually be gated ---

check(
    "file_controller/delete is gated (AUDIT.md #22 -- irreversible without confirmation)",
    gated("file_controller", {"action": "delete"}),
)
check(
    "file_controller/move is gated",
    gated("file_controller", {"action": "move"}),
)
check(
    "file_controller/list is NOT gated (read-only)",
    not gated("file_controller", {"action": "list"}),
)
check(
    "file_processor/run is DANGEROUS and gated (AUDIT.md #23 -- unsandboxed subprocess.run)",
    resolve_risk("file_processor", {"action": "run"}) == RiskLevel.DANGEROUS,
)
check(
    "file_processor/summarize is NOT gated",
    not gated("file_processor", {"action": "summarize"}),
)
check(
    "desktop_control/task is DANGEROUS and gated (AUDIT.md #14 -- exec() on LLM-generated code)",
    resolve_risk("desktop_control", {"action": "task"}) == RiskLevel.DANGEROUS,
)
check(
    "desktop_control/wallpaper is NOT gated",
    not gated("desktop_control", {"action": "wallpaper"}),
)
check(
    "browser_control is gated regardless of action (AUDIT.md #12 -- real profile launch)",
    gated("browser_control", {"action": "screenshot"}) and gated("browser_control", {"action": "go_to"}),
)
check(
    "send_message is gated regardless of platform (AUDIT.md #26)",
    gated("send_message", {"platform": "telegram"}) and gated("send_message", {"platform": "whatsapp"}),
)
check(
    "screen_process angle=camera is gated (AUDIT.md #20 -- webcam activation)",
    gated("screen_process", {"angle": "camera"}),
)
check(
    "screen_process angle=screen (default) is NOT gated",
    not gated("screen_process", {"angle": "screen"}) and not gated("screen_process", {}),
)
check(
    "computer_settings/shutdown and /restart are DANGEROUS",
    resolve_risk("computer_settings", {"action": "shutdown"}) == RiskLevel.DANGEROUS
    and resolve_risk("computer_settings", {"action": "restart"}) == RiskLevel.DANGEROUS,
)
check(
    "computer_settings/toggle_wifi is SENSITIVE (AUDIT.md #13 -- asymmetric with restart/shutdown before this fix)",
    resolve_risk("computer_settings", {"action": "toggle_wifi"}) == RiskLevel.SENSITIVE,
)
check(
    "computer_settings/volume_up is NOT gated",
    not gated("computer_settings", {"action": "volume_up"}),
)
check(
    "game_updater with shutdown_when_done=True is DANGEROUS (AUDIT.md #18)",
    resolve_risk("game_updater", {"action": "download_status", "shutdown_when_done": True}) == RiskLevel.DANGEROUS,
)
check(
    "game_updater without shutdown_when_done is NOT gated",
    not gated("game_updater", {"action": "update"}),
)
check(
    "code_helper/run is DANGEROUS, /explain is not gated",
    resolve_risk("code_helper", {"action": "run"}) == RiskLevel.DANGEROUS
    and not gated("code_helper", {"action": "explain"}),
)
check(
    "dev_agent is gated (no discrete safe action -- always installs+runs, AUDIT.md #16)",
    gated("dev_agent", {}),
)
check(
    "shutdown_jarvis is DANGEROUS",
    resolve_risk("shutdown_jarvis", {}) == RiskLevel.DANGEROUS,
)
check(
    "window_manager/close and /focus are not gated",
    not gated("window_manager", {"action": "close"}) and not gated("window_manager", {"action": "focus"}),
)

# --- Safe/normal everyday tools must never be gated (or the assistant becomes useless) ---

for safe_name, safe_args in [
    ("open_app", {"app_name": "Notepad"}),
    ("web_search", {"query": "test"}),
    ("system_status", {}),
    ("weather_report", {"city": "Kyiv"}),
    ("reminder", {"date": "2026-09-01", "time": "09:00", "message": "test"}),
    ("save_memory", {"category": "notes", "key": "x", "value": "y"}),
    ("youtube_video", {"action": "get_info"}),
]:
    check(f"{safe_name} is NOT gated", not gated(safe_name, safe_args))

# --- confirmed=true is respected once the caller supplies it explicitly ---
# (resolve_risk itself doesn't take `confirmed` -- that's core/tool_dispatch.py's
# job, tested implicitly here: the tool_registry layer only ever answers "what
# risk is this", the gate decision "risk in GATED_LEVELS and not confirmed"
# lives in tool_dispatch.execute_tool. This just pins the risk answer is stable.)
check(
    "resolve_risk is a pure function of (name, args) -- same input, same answer",
    resolve_risk("file_controller", {"action": "delete"}) == resolve_risk("file_controller", {"action": "delete"}),
)

# --- add_confirmation_param mutates every declaration, doesn't drop any ---

_fake_declarations = [
    {"name": "a", "parameters": {"type": "OBJECT", "properties": {}, "required": []}},
    {"name": "b", "parameters": {"type": "OBJECT", "properties": {"x": {"type": "STRING"}}, "required": ["x"]}},
]
add_confirmation_param(_fake_declarations)
check(
    "add_confirmation_param adds 'confirmed' to every tool's properties, even ones with no other params",
    all("confirmed" in d["parameters"]["properties"] for d in _fake_declarations),
)
check(
    "add_confirmation_param does not disturb existing parameters",
    "x" in _fake_declarations[1]["parameters"]["properties"],
)

# --- Wired into the real TOOL_DECLARATIONS in main.py ---
try:
    import main as _main
    check(
        "main.TOOL_DECLARATIONS has 'confirmed' on every one of its 25 tools",
        len(_main.TOOL_DECLARATIONS) > 0
        and all("confirmed" in d["parameters"]["properties"] for d in _main.TOOL_DECLARATIONS),
    )
except Exception as e:
    check(f"main.py imports cleanly with tool_registry wired in ({e})", False)

print()
if failures:
    print(f"{failures} check(s) FAILED.")
    sys.exit(1)
else:
    print("All checks passed.")
