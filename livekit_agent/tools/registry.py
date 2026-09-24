"""Two small registries that decouple LLM-facing tools from the scenario engine.

`IMPL_REGISTRY` maps a stable string name -> the underlying async implementation
(no RunContext, keyword-only args, returns a dict). `run_scenario` calls these
directly so replaying a saved scenario never goes back through the LLM.

`FUNCTION_TOOLS` collects the `@function_tool`-wrapped functions that get
handed to the `Agent` so the LLM can call them during a live conversation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

ToolImpl = Callable[..., Awaitable[dict[str, Any]]]

IMPL_REGISTRY: dict[str, ToolImpl] = {}
FUNCTION_TOOLS: list[Any] = []


def register_impl(name: str) -> Callable[[ToolImpl], ToolImpl]:
    """Register `fn` under `name` in IMPL_REGISTRY so scenarios can call it by name."""

    def deco(fn: ToolImpl) -> ToolImpl:
        if name in IMPL_REGISTRY:
            raise ValueError(f"duplicate tool implementation name: {name!r}")
        IMPL_REGISTRY[name] = fn
        return fn

    return deco


def register_tool(tool: Any) -> Any:
    """Append an `@function_tool`-decorated callable to FUNCTION_TOOLS."""
    FUNCTION_TOOLS.append(tool)
    return tool
