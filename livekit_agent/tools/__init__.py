"""Importing this package registers every tool (see registry.py for how).

`worker.py` only needs `FUNCTION_TOOLS` (for the Agent) and, indirectly via
scenarios.py, `IMPL_REGISTRY` (for replaying saved scenarios).
"""

from __future__ import annotations

import screens  # noqa: F401  (per-monitor DPI awareness before anything imports pyautogui)
from tools.registry import FUNCTION_TOOLS, IMPL_REGISTRY

from tools import (  # noqa: F401,E402  (imported for their registration side effects)
    adobe_after_effects,
    adobe_photoshop,
    adobe_premiere,
    briefing,
    browser,
    camera,
    coding_agent,
    computer_use,
    day_plan,
    desktop,
    dev,
    email_tools,
    file_ops,
    info,
    led_strip,
    media,
    memory,
    monitors,
    notes,
    quick_ui,
    scenarios,
    scheduling,
    screen_watch,
    security,
    shell,
    smart_home,
    system,
    tasks,
    tg_chats,
    telegram_dm,
    triggers,
    voice_control,
    web,
    window_control,
)

__all__ = ["FUNCTION_TOOLS", "IMPL_REGISTRY"]
