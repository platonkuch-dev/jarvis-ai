"""A tiny MCP server that hands Python tools to the local `claude` CLI.

Used two ways under LLM_PROVIDER=claude_code:
  - JarvisMCPServer serves Jarvis's own function tools (tools.FUNCTION_TOOLS)
    to the voice brain, so it keeps every Jarvis ability (browser_task,
    use_computer, memory, change_voice, Telegram, ...) next to Claude Code's
    built-in tools.
  - cc_agent.py starts a throwaway MCPServer per sub-agent run (the screen
    agent's mouse/keyboard, the browser agent's page actions, a background
    task's tools).

It listens on 127.0.0.1 only, on a random port, behind a random bearer token,
and speaks the minimal slice of MCP's streamable-HTTP transport that Claude
Code needs: one JSON-RPC request per POST, answered with plain JSON (no SSE,
no server-initiated messages). Tools run in the caller's own event loop.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiohttp import web
from livekit.agents import RunContext, llm
from livekit.agents.llm import utils as llm_utils

logger = logging.getLogger("jarvis-voice-agent.mcp")

SERVER_NAME = "jarvis"
_DEFAULT_PROTOCOL = "2025-06-18"


class ToolFailed(Exception):
    """Raised by a handler: reported to the model as an error result."""


@dataclass
class MCPTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    # Returns a str, a JSON-able dict, or a list of content blocks (MCP
    # {"type":"text"|"image",...} or Anthropic-style image blocks).
    handler: Callable[[dict[str, Any]], Awaitable[Any]]


class MCPServer:
    def __init__(self, tools: list[MCPTool], *, name: str = SERVER_NAME) -> None:
        self.name = name
        self._tools = {t.name: t for t in tools}
        self._token = secrets.token_urlsafe(32)
        self._runner: web.AppRunner | None = None
        self.url = ""

    def mcp_config(self) -> dict[str, Any]:
        """The --mcp-config JSON that points `claude` at this server."""
        return {
            "mcpServers": {
                self.name: {
                    "type": "http",
                    "url": self.url,
                    "headers": {"Authorization": f"Bearer {self._token}"},
                }
            }
        }

    async def start(self) -> None:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_post("/mcp", self._handle)
        app.router.add_get("/mcp", lambda _r: web.Response(status=405))
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.url = f"http://127.0.0.1:{port}/mcp"
        logger.info("MCP server %r with %d tools on %s", self.name, len(self._tools), self.url)

    async def aclose(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _handle(self, request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != f"Bearer {self._token}":
            return web.Response(status=401)
        try:
            msg = await request.json()
        except ValueError:
            return _rpc_error(None, -32700, "parse error")
        if isinstance(msg, list):  # batches aren't used by Claude Code
            return _rpc_error(None, -32600, "batch requests are not supported")
        if "id" not in msg:  # notification (notifications/initialized etc.)
            return web.Response(status=202)
        rid, method, params = msg["id"], msg.get("method"), msg.get("params") or {}
        if method == "initialize":
            return _rpc_result(rid, {
                "protocolVersion": params.get("protocolVersion", _DEFAULT_PROTOCOL),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.name, "version": "1.0.0"},
            })
        if method == "ping":
            return _rpc_result(rid, {})
        if method == "tools/list":
            return _rpc_result(rid, {"tools": [
                {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
                for t in self._tools.values()
            ]})
        if method == "tools/call":
            return _rpc_result(rid, await self._call(params.get("name", ""), params.get("arguments") or {}))
        return _rpc_error(rid, -32601, f"method not found: {method}")

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            return _result([_text(f"Нет такого инструмента: {name}")], error=True)
        logger.info("mcp %s call: %s %s", self.name, name, json.dumps(arguments, ensure_ascii=False)[:300])
        try:
            result = await tool.handler(arguments)
        except ToolFailed as exc:
            return _result([_text(str(exc))], error=True)
        except Exception as exc:
            logger.exception("mcp tool %s failed", name)
            return _result([_text(f"Инструмент {name} упал: {exc}")], error=True)
        return _result(to_content(result))


class JarvisMCPServer(MCPServer):
    """Jarvis's LiveKit function tools, exactly as the voice LLM would call them."""

    def __init__(self, function_tools: list[Any], extra: list[MCPTool] | None = None) -> None:
        super().__init__([_wrap_function_tool(t) for t in function_tools if llm.is_function_tool(t)]
                         + list(extra or []))


def _wrap_function_tool(tool: Any) -> MCPTool:
    schema = llm_utils.build_legacy_openai_schema(tool, internally_tagged=True)

    async def handler(arguments: dict[str, Any]) -> Any:
        try:
            # Jarvis tools take a RunContext first but never read it; there is
            # no LiveKit speech turn behind an MCP call to build a real one from.
            args, kwargs = llm_utils.prepare_function_arguments(
                fnc=tool, json_arguments=arguments, call_ctx=RunContext.__new__(RunContext)
            )
            result = await tool(*args, **kwargs)
        except llm.ToolError as exc:
            raise ToolFailed(str(exc)) from exc
        except llm.StopResponse:
            return "Готово."
        return "Готово." if result is None else result

    return MCPTool(schema["name"], schema["description"], schema["parameters"], handler)


def to_content(result: Any) -> list[dict[str, Any]]:
    """Handler return value -> MCP content blocks."""
    if isinstance(result, str):
        return [_text(result)]
    if isinstance(result, list):
        blocks = []
        for b in result:
            if isinstance(b, dict) and b.get("type") == "image" and "source" in b:  # Anthropic-style
                src = b["source"]
                blocks.append({"type": "image", "data": src["data"], "mimeType": src["media_type"]})
            elif isinstance(b, dict) and b.get("type") in ("text", "image"):
                blocks.append(b)
            else:
                blocks.append(_text(str(b)))
        return blocks
    return [_text(json.dumps(result, ensure_ascii=False, default=str))]


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _result(content: list[dict[str, Any]], *, error: bool = False) -> dict[str, Any]:
    return {"content": content, "isError": error}


def _rpc_result(rid: Any, result: dict[str, Any]) -> web.Response:
    return web.json_response({"jsonrpc": "2.0", "id": rid, "result": result})


def _rpc_error(rid: Any, code: int, message: str) -> web.Response:
    return web.json_response({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})
