"""Runs simple, tool-free text prompts through the local `claude` CLI's
non-interactive print mode (`claude -p`) instead of the Anthropic Messages
API. With ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN stripped from the
subprocess's environment, the CLI falls back to whatever auth `claude`
already has stored locally (a Claude.ai Pro/Max subscription login, if
that's what this machine is set up with) -- so calls made through here bill
against that subscription's included usage instead of this project's
metered API key. Verified live: the CLI answers correctly with zero
Anthropic env vars present.

Only fit for infrequent, tool-free, latency-insensitive background calls
(memory consolidation, pattern-learning analysis) -- NOT the real-time voice
loop or the interactive computer-use loop, which need low latency and
structured tool-calling that this doesn't provide.
"""

from __future__ import annotations

import asyncio
import logging
import os

import config

logger = logging.getLogger("jarvis-voice-agent.claude_cli")

_TIMEOUT = 60.0


async def ask(prompt: str) -> str | None:
    """Returns the CLI's reply text, or None if `claude` isn't installed,
    times out, or exits non-zero (callers should fall back to their own
    direct-API path on None). Always None unless config.USE_CLAUDE_CLI is on
    or the whole app runs on the subscription (config.SUBSCRIPTION_MODE)."""
    if config.SUBSCRIPTION_MODE:
        import cc_agent

        try:
            return await cc_agent.ask(prompt, model=config.CLAUDE_CODE_FAST_MODEL, timeout=_TIMEOUT * 2)
        except Exception as exc:
            logger.warning("claude CLI call failed: %s", exc)
            return None
    if not config.USE_CLAUDE_CLI:
        return None
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=config.NO_WINDOW,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
    except (FileNotFoundError, OSError, asyncio.TimeoutError) as exc:
        logger.warning("claude CLI call failed: %s", exc)
        return None
    if proc.returncode != 0:
        logger.warning("claude CLI exited %d: %s", proc.returncode, stderr.decode(errors="replace")[:300])
        return None
    return stdout.decode(errors="replace").strip()
