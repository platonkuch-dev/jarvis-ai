"""Headless Claude tool-use loop over the same tools the voice agent has.

Used wherever there's no live voice session to drive the conversation: the
Telegram chat bridge (owner turns) and background tasks (tasks.py). Tools
run through IMPL_REGISTRY -- the exact functions the voice LLM calls -- so
behaviour, logging and safety checks are identical on every path.

`gate` lets the caller veto or approve each call before it runs (tasks.py
plugs policy.py + Telegram approvals in here); `extra_tools` handles tools
that exist only on one path (the bridge's remember_contact_note).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import anthropic

import config
import usage
from tools import FUNCTION_TOOLS
from tools.registry import IMPL_REGISTRY

Gate = Callable[[str, dict], Awaitable[str | None]]  # -> None to allow, or a refusal message
ExtraTool = Callable[[dict], Awaitable[dict]]

_client: anthropic.AsyncAnthropic | None = None
_schema_cache: dict[frozenset, list[dict]] = {}


def client() -> anthropic.AsyncAnthropic:
    # One client for the life of the process: a fresh one per turn would open
    # a new connection pool (TCP+TLS handshake) every time.
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def tool_schemas(exclude: frozenset[str] = frozenset()) -> list[dict]:
    """FUNCTION_TOOLS as Anthropic tool definitions (cached: they never change
    after start-up), minus `exclude`. The last one carries cache_control so
    the whole tool block is cached across calls."""
    if exclude not in _schema_cache:
        from livekit.agents.llm import ToolContext
        from livekit.agents.llm._provider_format.anthropic import to_fnc_ctx

        result = to_fnc_ctx(ToolContext(FUNCTION_TOOLS), strict=False)
        schemas = result[0] if isinstance(result, tuple) else result
        schemas = [dict(s) for s in schemas if s.get("name") not in exclude]
        for s in schemas:
            s.pop("cache_control", None)
        if schemas:
            schemas[-1]["cache_control"] = {"type": "ephemeral"}
        _schema_cache[exclude] = schemas
    return _schema_cache[exclude]


async def _call_tool(name: str, args: dict, gate: Gate | None, extra_tools: dict[str, ExtraTool]) -> dict:
    if name in extra_tools:
        return await extra_tools[name](args)
    impl = IMPL_REGISTRY.get(name)
    if impl is None:
        return {"status": "error", "message": f"Инструмент «{name}» не найден."}
    if gate is not None:
        refusal = await gate(name, args)
        if refusal:
            return {"status": "denied", "message": refusal}
    try:
        result = await impl(**args)
        return result if isinstance(result, dict) else {"status": "ok", "message": str(result)}
    except TypeError as exc:
        return {"status": "error", "message": f"Неверные аргументы для {name}: {exc}"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


async def run(
    *,
    system_text: str,
    tools_param: list[dict],
    history: list[dict],
    user_text: str,
    model: str | None = None,
    max_steps: int = 8,
    source: str = "agent",
    gate: Gate | None = None,
    extra_tools: dict[str, ExtraTool] | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_step: Callable[[str], None] | None = None,
) -> tuple[str, list[dict]]:
    """Runs until the model answers without calling a tool. Returns (final
    text, full message list including `history`)."""
    model = model or config.ANTHROPIC_MODEL
    extra_tools = extra_tools or {}
    system_blocks = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
    messages = history + [{"role": "user", "content": user_text}]

    for _ in range(max_steps):
        if should_stop is not None and should_stop():
            return "Остановлено.", messages
        response = await client().messages.create(
            model=model,
            max_tokens=2048,
            system=system_blocks,
            tools=tools_param,
            messages=messages,
        )
        usage.record_response(model, response.usage, source=source)
        messages.append({"role": "assistant", "content": [b.model_dump() for b in response.content]})

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            final_text = "".join(b.text for b in response.content if b.type == "text").strip()
            return final_text or "...", messages

        result_blocks = []
        for tu in tool_uses:
            args = tu.input if isinstance(tu.input, dict) else {}
            if on_step is not None:
                on_step(f"{tu.name}({json.dumps(args, ensure_ascii=False)[:200]})")
            result = await _call_tool(tu.name, args, gate, extra_tools)
            result_blocks.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": json.dumps(result, ensure_ascii=False, default=str)[:8000],
                **({"is_error": True} if result.get("status") in ("error", "denied") else {}),
            })
        messages.append({"role": "user", "content": result_blocks})

    return "Не получилось закончить за разумное число шагов.", messages
