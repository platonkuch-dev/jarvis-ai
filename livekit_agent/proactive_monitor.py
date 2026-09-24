"""Standalone daemon: watches the machine's basic health (disk space, memory,
recent errors in Jarvis's own log) and messages the owner on Telegram when
something crosses a threshold -- proactive, not asked for.

Pure Python + psutil, no Claude calls at all: this is a free background
feature, deliberately kept that way. Runs as its own long-lived process
(started by app.py alongside the others) with its own Telegram session copy
(bootstrapped from the main one) so it never contends with the voice
agent's send-only client or the chat bridge's listening connection over the
same local session file.

It is also the delivery end of notify.py's outbox: every few seconds it sends
whatever other processes queued for the owner (reminders, background task
results, approval questions, supervisor alerts).

Each check alerts once when a condition first crosses its threshold, then
stays quiet until the condition clears and re-crosses -- otherwise every
5-minute tick while low disk space persists would repeat the same message
forever.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path

import psutil
from dotenv import load_dotenv

load_dotenv()

import config
import notify
import telegram_owner
from telethon import TelegramClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jarvis-voice-agent.proactive_monitor")

_alert_state: dict[str, bool] = {}
# Byte offset into app.log already scanned. Starts at the file's current end,
# so a restart never re-reports errors that were already there.
_log_offset: int | None = None

# How often the notify.py outbox (reminders, task results, approval
# questions, supervisor alerts) is delivered -- much tighter than the health
# checks, since an approval question is waiting on it.
_OUTBOX_INTERVAL_S = 3.0


def _check_disk() -> str | None:
    usage = psutil.disk_usage(str(config.BASE_DIR))
    free_pct = 100 - usage.percent
    key = "disk"
    if free_pct < config.MONITOR_DISK_FREE_PCT_THRESHOLD:
        if not _alert_state.get(key):
            _alert_state[key] = True
            free_gb = usage.free / (1024**3)
            return f"Мало места на диске: свободно {free_pct:.1f}% ({free_gb:.1f} ГБ)."
    else:
        _alert_state[key] = False
    return None


def _check_memory() -> str | None:
    percent = psutil.virtual_memory().percent
    key = "memory"
    if percent > config.MONITOR_MEMORY_PCT_THRESHOLD:
        if not _alert_state.get(key):
            _alert_state[key] = True
            return f"Высокая загрузка памяти: {percent:.0f}%."
    else:
        _alert_state[key] = False
    return None


def _read_new_log_lines(log_path: Path) -> list[str]:
    global _log_offset
    size = log_path.stat().st_size
    if _log_offset is None:
        _log_offset = size
        return []
    if size < _log_offset:  # rotated since the last look
        _log_offset = 0
    with open(log_path, "rb") as f:
        f.seek(_log_offset)
        chunk = f.read()
    _log_offset = size
    return chunk.decode("utf-8", errors="replace").splitlines()


def _check_recent_errors() -> str | None:
    log_path = config.LOGS_DIR / "app.log"
    if not log_path.exists():
        return None
    window = _read_new_log_lines(log_path)[-config.MONITOR_LOG_ERROR_WINDOW :]

    blob = "\n".join(window)
    if not ("ERROR" in blob or "Traceback" in blob):
        return None
    # STT/TTS providers drop their websocket occasionally under normal
    # network conditions -- livekit-agents already retries these itself
    # (that's what retryable=True means), so alerting on every one would
    # just be noise about something that self-heals. Only a traceback
    # WITHOUT that marker is a real, unhandled problem worth a message.
    if "retryable=True" in blob:
        return None

    # The log's rich-console formatting wraps long lines across several
    # physical ones, so "the last matching line" is often just the
    # "Traceback (most recent call last):" header, not the actual
    # exception. Send a real chunk of context instead of guessing which
    # single line matters.
    preview = "\n".join(window[-12:])[-500:]
    return f"В логе Джарвиса новая необработанная ошибка:\n{preview}"


_CHECKS = [_check_disk, _check_memory, _check_recent_errors]


async def _send_alert(client: TelegramClient, owner_id: int, text: str) -> None:
    try:
        await client.send_message(owner_id, f"[Мониторинг] {text}")
    except Exception:
        logger.exception("failed to send monitor alert")


async def main() -> None:
    if not config.TELEGRAM_API_ID or not config.TELEGRAM_API_HASH:
        logger.error("TELEGRAM_API_ID/TELEGRAM_API_HASH not set -- see .env")
        return

    monitor_session = Path(config.TELEGRAM_MONITOR_SESSION_PATH + ".session")
    main_session = Path(config.TELEGRAM_SESSION_PATH + ".session")
    if not monitor_session.exists() and main_session.exists():
        shutil.copyfile(main_session, monitor_session)
        logger.info("bootstrapped monitor session from the main Telegram session")

    client = TelegramClient(
        config.TELEGRAM_MONITOR_SESSION_PATH, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH
    )
    await client.connect()

    logger.info("proactive monitor running, checks every %.0fs, outbox every %.0fs",
                config.MONITOR_CHECK_INTERVAL_S, _OUTBOX_INTERVAL_S)

    last_checks = 0.0
    while True:
        owner_id = telegram_owner.load_owner_id()
        if owner_id is not None and await client.is_user_authorized():
            await _deliver_outbox(client, owner_id)
            if time.time() - last_checks >= config.MONITOR_CHECK_INTERVAL_S:
                last_checks = time.time()
                for check in _CHECKS:
                    try:
                        message = check()
                    except Exception:
                        logger.exception("check %s failed", check.__name__)
                        continue
                    if message:
                        await _send_alert(client, owner_id, message)
        await asyncio.sleep(_OUTBOX_INTERVAL_S)


async def _deliver_outbox(client: TelegramClient, owner_id: int) -> None:
    entries = notify.drain()
    for i, entry in enumerate(entries):
        try:
            await client.send_message(owner_id, entry["text"])
        except Exception:
            logger.exception("outbox delivery failed -- requeueing %d message(s)", len(entries) - i)
            notify.requeue(entries[i:])
            return


if __name__ == "__main__":
    asyncio.run(main())
