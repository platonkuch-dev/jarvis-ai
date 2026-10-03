"""One Telegram session file per process.

Telethon keeps its session in SQLite and writes to it while connected
(entity cache, update state). Two processes on the same file fight over the
write lock -- "database is locked" in the logs, a failed send, a crashed
bridge. So every long-running process gets its own byte copy of the main
login (config.TELEGRAM_SESSION_PATH): same auth key, separate file. The main
file itself is only ever written by the login flow and read as the copy
source.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
from pathlib import Path

import config

logger = logging.getLogger("jarvis-voice-agent.tg_session")

# Every per-process copy ever handed out, so the panel can drop them all
# after a re-login (a stale copy would silently keep the old account).
COPY_SUFFIXES = ("_bridge", "_monitor", "_archive", "_worker_console", "_worker_phone")


def own_copy(path: str, source: str | None = None) -> str:
    """Make sure `path`.session exists (copied from `source`.session, the
    main login by default) and return `path` for TelegramClient."""
    source = source or config.TELEGRAM_SESSION_PATH
    target, origin = Path(path + ".session"), Path(source + ".session")
    if not target.exists() and origin.exists():
        shutil.copyfile(origin, target)
        logger.info("bootstrapped %s from %s", target.name, origin.name)
    return path


async def connect_with_retry(client, attempts: int = 6, start: bool = False, **start_kwargs) -> None:
    """client.connect()/start() with a short backoff on a transient SQLite
    lock (an antivirus or indexer scan touching the .session file) instead
    of failing the call or crashing the process."""
    for attempt in range(attempts):
        try:
            if start:
                await client.start(**start_kwargs)
            else:
                await client.connect()
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == attempts - 1:
                raise
            delay = min(2 ** attempt, 8)
            logger.warning("session database locked (attempt %d/%d) -- retrying in %ds",
                           attempt + 1, attempts, delay)
            await asyncio.sleep(delay)
