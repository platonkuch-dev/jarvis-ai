"""
Risk classification for every tool registered in main.py's TOOL_DECLARATIONS,
and the confirmation gate that keeps SENSITIVE/DANGEROUS actions from
executing on the first recognized word.

Ports the SAFE/NORMAL/SENSITIVE/DANGEROUS principle from
agent-ts/PermissionManager.ts to the Python side (see REWORK_PLAN.md §3),
with one deliberate change from that TypeScript version: there, only
DANGEROUS is gated and SENSITIVE runs autonomously. AUDIT.md's Stage-1
findings showed that most of the concretely risky Python-side actions
(file move/write, browser_control's real-profile launch, send_message,
...) are SENSITIVE, not DANGEROUS, by this scheme -- gating only DANGEROUS
would leave nearly every finding from the audit unprotected. So here BOTH
SENSITIVE and DANGEROUS require confirmation before they execute.

How confirmation works: Gemini Live's function-calling is turn-based --
there's no way to "pause" mid tool-call and wait for a plain-text answer.
So gating is implemented as a `confirmed` boolean parameter, appended to
every tool's schema in main.py (see `_add_confirmation_param`). The first
call to a gated action returns a [CONFIRMATION_REQUIRED] result instead of
running the handler; the tool description instructs Gemini to relay that
to the user in their own language, then -- ONLY if the user clearly
confirms -- call the exact same tool again with confirmed=true. Nothing
executes until that second call arrives.
"""
from __future__ import annotations

from enum import IntEnum


class RiskLevel(IntEnum):
    SAFE = 0        # read-only or trivially reversible
    NORMAL = 1      # routine, expected, reversible
    SENSITIVE = 2   # harder to reverse or affects more than the target
    DANGEROUS = 3   # explicitly gated, most severe class


# Risk levels remain available for logging and diagnostics, but this local
# installation runs every requested action immediately.
GATED_LEVELS = frozenset()


# Per-tool default risk level, used when the tool has no discrete
# sub-actions or the call's action value isn't in ACTION_RISK_OVERRIDES.
# Classification source: AUDIT.md (2026-08-30) + REWORK_PLAN.md §3's
# starting classification table.
TOOL_DEFAULT_RISK: dict[str, RiskLevel] = {
    "open_app":          RiskLevel.NORMAL,
    "web_search":        RiskLevel.SAFE,
    "system_status":     RiskLevel.SAFE,
    "process_hunter":    RiskLevel.SAFE,         # read-only scan, no kill/action performed
    "hacker_terminal":   RiskLevel.DANGEROUS,    # arbitrary voice-triggered shell -- was force-gated locally, disabled at the user's request 2026-09-03 (see core/tool_dispatch.py's _LOCALLY_GATED_TOOLS)
    "digital_ghost":     RiskLevel.SAFE,         # read-only baseline/delta query, no side effects
    "weather_report":    RiskLevel.SAFE,
    "send_message":      RiskLevel.SENSITIVE,   # gated regardless of platform -- AUDIT.md #26
    "reminder":          RiskLevel.NORMAL,
    "youtube_video":     RiskLevel.NORMAL,       # 'play' opens an app/browser, like open_app
    "send_screenshot":   RiskLevel.NORMAL,       # leaves the machine, but only to the chat already talking to JARVIS
    "screen_process":    RiskLevel.SAFE,         # overridden per angle below -- camera is SENSITIVE
    "screen_watch":      RiskLevel.SAFE,
    "close_camera":      RiskLevel.SAFE,
    "computer_settings": RiskLevel.NORMAL,       # overridden per action below
    "browser_control":   RiskLevel.SENSITIVE,    # launches on the REAL browser profile by default -- AUDIT.md #12
    "file_controller":   RiskLevel.NORMAL,       # overridden per action below
    "desktop_control":   RiskLevel.NORMAL,       # overridden per action below
    "code_helper":       RiskLevel.SAFE,         # overridden per action below -- run/build execute code
    "dev_agent":         RiskLevel.SENSITIVE,    # always installs deps + runs generated code -- AUDIT.md #16
    "computer_control":  RiskLevel.NORMAL,       # overridden per action below
    "computer_agent":    RiskLevel.SENSITIVE,    # autonomous multi-step clicking/typing driven by a vision model; action=stop is SAFE
    "window_manager":    RiskLevel.SAFE,         # overridden per action below -- close/kill are SENSITIVE
    "ui_automation":     RiskLevel.SAFE,         # overridden per action below
    "game_updater":      RiskLevel.NORMAL,       # shutdown_when_done=true overrides to DANGEROUS, see resolve_risk()
    "flight_finder":     RiskLevel.NORMAL,       # opens a real, visible browser window each call
    "shutdown_jarvis":   RiskLevel.DANGEROUS,    # kills the assistant process outright
    "file_processor":    RiskLevel.NORMAL,       # overridden per action below -- run is DANGEROUS
    "save_memory":       RiskLevel.NORMAL,       # handled before the gate in tool_dispatch.py anyway
    "add_capability":    RiskLevel.DANGEROUS,    # writes and wires in new code via Claude -- was force-gated locally, disabled at the user's request 2026-09-03 (see core/tool_dispatch.py's _LOCALLY_GATED_TOOLS)
}

# Per-tool action-specific overrides: {tool_name: (action_param_name, {value: RiskLevel})}.
# `value` is matched case-insensitively against the argument named by
# action_param_name. A tool absent here, or an action value not listed,
# falls back to TOOL_DEFAULT_RISK.
ACTION_RISK_OVERRIDES: dict[str, tuple[str, dict[str, RiskLevel]]] = {
    "screen_process": ("angle", {
        "camera": RiskLevel.SENSITIVE,   # activates the webcam -- AUDIT.md #20
    }),
    "computer_settings": ("action", {
        "restart": RiskLevel.DANGEROUS, "shutdown": RiskLevel.DANGEROUS,
        "toggle_wifi": RiskLevel.SENSITIVE, "wifi": RiskLevel.SENSITIVE,
        "dark_mode": RiskLevel.SENSITIVE,
        "lock_screen": RiskLevel.SENSITIVE, "lock": RiskLevel.SENSITIVE,
        "sleep_display": RiskLevel.SENSITIVE, "sleep": RiskLevel.SENSITIVE,
    }),
    "file_controller": ("action", {
        "delete": RiskLevel.SENSITIVE, "move": RiskLevel.SENSITIVE,
        "write": RiskLevel.SENSITIVE, "rename": RiskLevel.SENSITIVE,
        "organize_desktop": RiskLevel.SENSITIVE,
        "list": RiskLevel.SAFE, "read": RiskLevel.SAFE, "find": RiskLevel.SAFE,
        "largest": RiskLevel.SAFE, "disk_usage": RiskLevel.SAFE, "info": RiskLevel.SAFE,
        "create_file": RiskLevel.NORMAL, "create_folder": RiskLevel.NORMAL, "copy": RiskLevel.NORMAL,
    }),
    "desktop_control": ("action", {
        "organize": RiskLevel.SENSITIVE, "clean": RiskLevel.SENSITIVE,
        "task": RiskLevel.DANGEROUS,   # LLM-generated code through exec() -- AUDIT.md #14
        "wallpaper": RiskLevel.NORMAL, "wallpaper_url": RiskLevel.NORMAL,
        "list": RiskLevel.SAFE, "stats": RiskLevel.SAFE,
    }),
    "code_helper": ("action", {
        "run": RiskLevel.DANGEROUS, "build": RiskLevel.SENSITIVE,
        "write": RiskLevel.NORMAL, "edit": RiskLevel.NORMAL,
        "explain": RiskLevel.SAFE, "auto": RiskLevel.SAFE,
    }),
    "computer_control": ("action", {
        "screenshot": RiskLevel.SAFE, "wait": RiskLevel.SAFE,
        "random_data": RiskLevel.SAFE, "user_data": RiskLevel.SAFE,
        "focus_window": RiskLevel.SAFE,
    }),
    "computer_agent": ("action", {
        "stop": RiskLevel.SAFE,
    }),
    "window_manager": ("action", {
        "close": RiskLevel.NORMAL,
        "move": RiskLevel.NORMAL, "resize": RiskLevel.NORMAL,
        "list_windows": RiskLevel.SAFE, "list_processes": RiskLevel.SAFE,
        "get_active": RiskLevel.SAFE, "focus": RiskLevel.SAFE,
        "minimize": RiskLevel.SAFE, "maximize": RiskLevel.SAFE, "restore": RiskLevel.SAFE,
        "wait_for_window": RiskLevel.SAFE,
    }),
    "ui_automation": ("action", {
        "click": RiskLevel.NORMAL, "double_click": RiskLevel.NORMAL,
        "right_click": RiskLevel.NORMAL, "type": RiskLevel.NORMAL,
        "select": RiskLevel.NORMAL, "clear": RiskLevel.NORMAL,
        "find": RiskLevel.SAFE, "read": RiskLevel.SAFE,
        "get_tree": RiskLevel.SAFE, "wait_for_element": RiskLevel.SAFE,
    }),
    "file_processor": ("action", {
        "run": RiskLevel.DANGEROUS,   # unsandboxed subprocess.run on an unvalidated path -- AUDIT.md #23
    }),
}


def resolve_risk(name: str, args: dict) -> RiskLevel:
    """The risk level for a specific call: TOOL_DEFAULT_RISK, refined by
    ACTION_RISK_OVERRIDES when the tool has an action-shaped parameter, plus
    the one non-action override (game_updater's shutdown_when_done)."""
    # game_updater's danger isn't which `action` was picked, it's whether
    # shutdown_when_done is set -- AUDIT.md #18 (real shutdown, no
    # re-confirmation at the moment the download actually finishes).
    if name == "game_updater" and args.get("shutdown_when_done"):
        return RiskLevel.DANGEROUS

    override = ACTION_RISK_OVERRIDES.get(name)
    if override:
        param_name, mapping = override
        value = str(args.get(param_name, "")).strip().lower()
        if value in mapping:
            return mapping[value]

    return TOOL_DEFAULT_RISK.get(name, RiskLevel.NORMAL)


def confirmation_prompt(name: str, args: dict, risk: RiskLevel) -> str:
    """Instruction text handed back to Gemini instead of a real result, when
    a gated action isn't confirmed yet. Gemini is expected to turn this into
    a natural spoken question in the user's own language -- this text is a
    directive to the model, not something spoken verbatim."""
    action = args.get("action") or args.get("angle") or ""
    what = f"{name}" + (f" (action={action})" if action else "")
    return (
        f"[CONFIRMATION_REQUIRED] '{what}' is classified {risk.name} and was NOT performed. "
        f"Ask the user, in their own language, to explicitly confirm they want you to proceed "
        f"with exactly this action, before doing anything else -- describe what it will do in "
        f"plain terms first. If, and only if, they clearly confirm (yes/да/confirm/etc.), call "
        f"{name} again with the exact same arguments plus confirmed=true. "
        f"If they decline or don't clearly confirm, do not call it again -- just acknowledge and move on."
    )


# Appended to every tool's JSON schema in main.py's TOOL_DECLARATIONS so
# Gemini has somewhere to put confirmed=true on a confirmed retry. Present
# on every tool (not just gated ones) so a reclassification never needs a
# matching schema edit -- it's a no-op extra parameter for SAFE/NORMAL tools.
CONFIRMED_PARAM_SCHEMA: dict = {
    "type": "BOOLEAN",
    "description": (
        "Only set this to true when calling this SAME tool again immediately after "
        "the user has just explicitly confirmed (said yes / da / confirm) in response "
        "to a confirmation question you asked them for this exact action. Never set it "
        "true on a first attempt or based on your own judgement."
    ),
}


def add_confirmation_param(tool_declarations: list[dict]) -> None:
    """Mutates each tool schema in place, adding the `confirmed` parameter."""
    for decl in tool_declarations:
        decl["parameters"]["properties"]["confirmed"] = CONFIRMED_PARAM_SCHEMA
