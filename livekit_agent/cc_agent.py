"""Runs one Claude Code sub-agent (`claude -p`) to completion, on the Claude.ai
subscription the CLI is logged into -- the LLM_PROVIDER=claude_code stand-in
for every direct Anthropic API call Jarvis used to make outside the voice
loop: the screen agent, the browser agent, background tasks, the Telegram
chat, camera/vision/screen-watch looks, memory consolidation.

`ask()` is a single question (optionally with images) and no tools.
`run()` is an agent: its tools are plain async Python handlers, served to
that one `claude` process over a throwaway local MCP server (jarvis_mcp.py);
Claude Code drives the loop itself, the handlers do the real work. A handler
can end the run early by raising Finish (the browser agent's "done", a
step/time cap); `should_stop` is polled too.

Claude Code's own built-in tools (shell, files, web) are switched off for
these runs: each sub-agent gets exactly the tools its caller hands it.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from typing import Any

import config
from jarvis_mcp import MCPServer, MCPTool, ToolFailed

logger = logging.getLogger("jarvis-voice-agent.cc_agent")

__all__ = ["MCPTool", "ToolFailed", "Finish", "ask", "ask_sync", "run", "image_block"]

_SERVER = "t"  # MCP server name: tools show up as mcp__t__<name>


class Finish(Exception):
    """Raised by a tool handler to end the run with `text` as its answer."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


class RunFailed(Exception):
    pass


def image_block(data: bytes, media_type: str = "image/jpeg") -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                        "data": base64.b64encode(data).decode("ascii")}}


async def ask(prompt: str, *, system: str = "", images: list[dict] | None = None,
              model: str | None = None, timeout: float = 120.0) -> str:
    """One question, no tools -> the answer text. Raises RunFailed."""
    return await run(prompt=prompt, system=system, images=images, model=model, timeout=timeout, max_turns=1)


def ask_sync(prompt: str, **kwargs: Any) -> str:
    """ask() for synchronous code (pyautogui loops, worker threads): runs it
    on a private event loop in its own thread, so it works whether or not
    the caller's thread already has a running loop."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, ask(prompt, **kwargs)).result()


async def run(
    *,
    prompt: str,
    system: str = "",
    images: list[dict] | None = None,
    tools: list[MCPTool] | None = None,
    model: str | None = None,
    max_turns: int | None = None,
    timeout: float | None = None,
    should_stop: Callable[[], bool] | None = None,
    thinking: bool = False,
) -> str:
    """Runs the agent until it answers without a tool call -> its final text.
    `images` are Anthropic-style image blocks sent along with `prompt`.
    Raises RunFailed if claude can't be run or fails outright."""
    exe = shutil.which("claude")
    if exe is None:
        raise RunFailed("claude CLI не найден в PATH")

    finish_text: list[str] = []
    finished = asyncio.Event()
    server = None
    wrapped = [_catch_finish(t, finish_text, finished) for t in tools or []]
    if wrapped:
        server = MCPServer(wrapped, name=_SERVER)
        await server.start()

    tmp = tempfile.TemporaryDirectory(prefix="jarvis_cc_", ignore_cleanup_errors=True)
    proc = None
    readers: list[asyncio.Task] = []
    try:
        prompt_file = os.path.join(tmp.name, "system.txt")
        with open(prompt_file, "w", encoding="utf-8") as f:
            f.write(system or "Отвечай по-русски.")
        args = [
            exe, "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json", "--verbose",
            "--model", model or config.CLAUDE_CODE_MODEL,
            "--system-prompt-file", prompt_file,
            "--tools", "",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--permission-mode", "default",
        ]
        if not thinking:
            args += ["--settings", json.dumps({"alwaysThinkingEnabled": False})]
        if max_turns:
            args += ["--max-turns", str(max_turns)]
        if server is not None:
            mcp_file = os.path.join(tmp.name, "mcp.json")
            with open(mcp_file, "w", encoding="utf-8") as f:
                json.dump(server.mcp_config(), f)
            args += ["--mcp-config", mcp_file, "--allowedTools", f"mcp__{_SERVER}"]

        env = os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)  # bill the subscription, not the API key
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        env["MCP_TOOL_TIMEOUT"] = str(int((timeout or 3600) * 1000))
        if not thinking:
            env["MAX_THINKING_TOKENS"] = "0"

        proc = await asyncio.create_subprocess_exec(
            *args, cwd=tmp.name, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=64 * 1024 * 1024,
            # The worker has no console (pythonw); without this Windows pops a
            # console window for every claude.exe it starts.
            creationflags=config.NO_WINDOW,
        )
        content: Any = prompt
        if images:
            content = [*images, {"type": "text", "text": prompt}]
        line = json.dumps({"type": "user", "message": {"role": "user", "content": content}}, ensure_ascii=False)
        proc.stdin.write((line + "\n").encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        result_task = asyncio.create_task(_read_result(proc))
        stderr_task = asyncio.create_task(proc.stderr.read())
        readers += [result_task, stderr_task]
        deadline = time.monotonic() + timeout if timeout else None
        finish_wait = asyncio.create_task(finished.wait())
        try:
            while True:
                done, _ = await asyncio.wait({result_task, finish_wait}, timeout=0.5,
                                             return_when=asyncio.FIRST_COMPLETED)
                if finish_wait in done:
                    return finish_text[0]
                if result_task in done:
                    break
                if should_stop is not None and should_stop():
                    return "Остановлено."
                if deadline is not None and time.monotonic() > deadline:
                    return "Не уложился во время — остановился."
        finally:
            finish_wait.cancel()

        event = result_task.result()
        if finish_text:
            return finish_text[0]
        if event is None:
            err = (await stderr_task).decode(errors="replace").strip()
            raise RunFailed(f"claude завершился без ответа: {err[:300]}")
        text = str(event.get("result") or "").strip()
        if event.get("is_error"):
            if event.get("subtype") == "error_max_turns":
                return text or "Не удалось закончить за разумное число шагов."
            raise RunFailed(text or event.get("subtype") or "ошибка claude")
        return text
    finally:
        if proc is not None:
            if proc.returncode is None:
                proc.kill()
            # Reap it and drain its pipes while this loop is still alive; a
            # transport left to the GC after asyncio.run() returns raises
            # "Event loop is closed".
            for task in readers:
                task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            transport = getattr(proc, "_transport", None)
            if transport is not None:
                transport.close()
        if server is not None:
            await server.aclose()
        tmp.cleanup()


async def _read_result(proc: asyncio.subprocess.Process) -> dict[str, Any] | None:
    assert proc.stdout is not None
    async for raw in proc.stdout:
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if event.get("type") == "result":
            return event
    return None


def _catch_finish(tool: MCPTool, sink: list[str], finished: asyncio.Event) -> MCPTool:
    async def handler(arguments: dict[str, Any]) -> Any:
        if finished.is_set():
            raise ToolFailed("Работа уже завершена.")
        try:
            return await tool.handler(arguments)
        except Finish as fin:
            sink.append(fin.text)
            finished.set()
            return "Готово, завершаю."

    return MCPTool(tool.name, tool.description, tool.input_schema, handler)
