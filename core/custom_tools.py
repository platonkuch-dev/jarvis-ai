"""
Registration and persistence for JARVIS's self-authored capabilities (see
actions/self_extend.py's `add_capability` tool, which is the only writer
of these).

A custom tool is: a JSON-schema declaration appended to main.py's
TOOL_DECLARATIONS list, a Python module saved under CUSTOM_TOOLS_DIR, and
a dispatch entry in core/tool_dispatch.py's `_CUSTOM_TOOL_HANDLERS`. This
module is the one place that knows how to do all three, both right after
a new tool is generated (live registration, from actions/self_extend.py)
and again at every startup (reload from disk, from main.py).

main.py owns TOOL_DECLARATIONS; actions/self_extend.py needs to append to
it without importing main.py itself, which would be circular (main.py
imports core.tool_dispatch, which imports actions.self_extend). Instead
main.py hands this module a reference once, via set_tool_declarations(),
right after TOOL_DECLARATIONS is finalized -- every function below reads
that stored reference rather than taking the list as a parameter.
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable

from core import tool_registry
from core.assistant_state import get_state, update_state, now_iso
from core.path_utils import get_user_data_dir

# %APPDATA%/Jarvis/custom_tools/<name>.py -- NOT actions/<name>.py. Generated
# source needs to survive a frozen PyInstaller build (get_user_data_dir() is
# the project's one established writable-and-persistent location, see
# core/assistant_state.py's STATE_PATH) and stay out of the source-controlled
# actions/ tree, which self-authored code was never meant to live in.
CUSTOM_TOOLS_DIR = get_user_data_dir() / "custom_tools"

_tool_declarations: list | None = None


def set_tool_declarations(tool_declarations: list) -> None:
    """Called once by main.py, right after TOOL_DECLARATIONS is built --
    see this module's docstring for why register_tool()/etc. don't just
    take the list as a parameter."""
    global _tool_declarations
    _tool_declarations = tool_declarations


@dataclass
class CustomToolSpec:
    name: str
    description: str
    parameters_schema: dict
    gated: bool
    created_at: str
    source_path: str


def load_persisted() -> list[CustomToolSpec]:
    entries = get_state().get("custom_tools", [])
    specs = []
    for entry in entries:
        try:
            specs.append(CustomToolSpec(**entry))
        except TypeError:
            continue  # a hand-edited or stale entry missing a field -- skip it, don't crash startup
    return specs


def save_persisted(spec: CustomToolSpec) -> None:
    def mutate(state):
        state["custom_tools"] = [
            e for e in state["custom_tools"] if e.get("name") != spec.name
        ]
        state["custom_tools"].append(asdict(spec))
    update_state(mutate)


def load_module_from_path(name: str, path: Path) -> Callable:
    """Imports a generated tool module as a real module (not a restricted
    exec() sandbox -- this needs to behave like any other first-party
    action module, including surviving restarts as one) and returns the
    callable matching `name` by the project's established handler-function
    convention (the function is named the same as the tool)."""
    spec = importlib.util.spec_from_file_location(f"jarvis_custom_tools.{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    handler = getattr(module, name, None)
    if handler is None:
        raise AttributeError(f"{path} has no function named '{name}'")
    return handler


def tool_exists(name: str) -> bool:
    """True if `name` is already a tool (built-in or custom) -- used by
    actions/self_extend.py before generating a new one, to reject a
    colliding name up front instead of silently shadowing an existing
    capability."""
    if _tool_declarations is None:
        return False
    return any(d.get("name") == name for d in _tool_declarations)


def register_tool(spec: CustomToolSpec, handler: Callable) -> None:
    """Wires one custom tool into the live process: appends its schema to
    main.TOOL_DECLARATIONS (via the reference set_tool_declarations()
    stored -- Gemini picks this up on the next reconnect, not mid-session,
    see main.py's _build_config()), registers its handler for dispatch,
    and applies its gating. Safe to call twice for the same name (e.g.
    once live, once again on the next startup's reload) -- replaces
    rather than duplicates."""
    if _tool_declarations is None:
        raise RuntimeError("core.custom_tools.set_tool_declarations() was never called")

    from core import tool_dispatch  # deferred: tool_dispatch imports every actions/* handler at module load

    decl = {
        "name": spec.name,
        "description": spec.description,
        "parameters": spec.parameters_schema,
    }
    tool_registry.add_confirmation_param([decl])
    _tool_declarations[:] = [d for d in _tool_declarations if d.get("name") != spec.name]
    _tool_declarations.append(decl)

    tool_dispatch._CUSTOM_TOOL_HANDLERS[spec.name] = handler
    if spec.gated:
        tool_dispatch._LOCALLY_GATED_TOOLS.add(spec.name)
    else:
        tool_dispatch._LOCALLY_GATED_TOOLS.discard(spec.name)


def reload_persisted_tools() -> None:
    """Called once at startup (main.py, right after
    set_tool_declarations()) so capabilities added in a previous session
    are usable again without re-generating them. One bad/missing file is
    logged and skipped rather than breaking startup for everything else."""
    for spec in load_persisted():
        try:
            handler = load_module_from_path(spec.name, Path(spec.source_path))
            register_tool(spec, handler)
            print(f"[CustomTools] Reloaded '{spec.name}'.")
        except Exception as e:
            print(f"[CustomTools] Skipped '{spec.name}': {e}")
