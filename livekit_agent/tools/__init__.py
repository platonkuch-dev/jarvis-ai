"""Importing this package registers every tool (see registry.py for how).

`worker.py` only needs `FUNCTION_TOOLS` (for the Agent) and, indirectly via
scenarios.py, `IMPL_REGISTRY` (for replaying saved scenarios).
"""

from __future__ import annotations

from tools.registry import FUNCTION_TOOLS, IMPL_REGISTRY

from tools import (  # noqa: F401,E402  (imported for their registration side effects)
    adobe_after_effects,
    adobe_photoshop,
    adobe_premiere,
    camera,
    coding_agent,
    computer_use,
    desktop,
    dev,
    file_ops,
    info,
    media,
    memory,
    notes,
    quick_ui,
    scenarios,
    scheduling,
    screen_watch,
    security,
    system,
    tasks,
    telegram_dm,
    triggers,
    voice_control,
    window_control,
)

__all__ = ["FUNCTION_TOOLS", "IMPL_REGISTRY"]
