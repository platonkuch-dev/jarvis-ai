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
import re
import time
from pathlib import Path

import psutil
from dotenv import load_dotenv

load_dotenv()

import config
import notify
import site_leads
import telegram_owner
import tg_session
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


# A record header in app.log (rich console format): "    23:51:52.450 ERROR    livekit.agents     Error in ...".
# Long messages wrap onto heavily indented continuation lines; tracebacks
# follow as unindented lines; JSON metadata ({"room": ..., "lk.pii.text": ...})
# closes the record.
_RECORD_RE = re.compile(r"^\s{0,8}\d{2}:\d{2}:\d{2}\.\d{3}\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(\S+)\s+(.*)$")
_EXC_RE = re.compile(r"^([\w.]+(?:Error|Exception|Exit|Interrupt|Failure))\b:?\s*(.*)$")
# Failures livekit-agents already retries or recovers from on its own (a
# dropped STT/TTS websocket, a TTS fallback switching voices): alerting on
# each would be noise about something that self-heals.
_SELF_HEALING = ("retryable=True", "all TTSs are unavailable", "recovery failed", "tts returned error",
                 "switching to next")


def _error_records(lines: list[str]) -> list[tuple[str, list[str]]]:
    """(header message, the record's following lines) for every ERROR/CRITICAL record."""
    records: list[tuple[str, str, list[str]]] = []
    for line in lines:
        m = _RECORD_RE.match(line)
        if m:
            records.append((m.group(1), m.group(3).strip(), []))
        elif records:
            records[-1][2].append(line)
    return [(msg, rest) for level, msg, rest in records if level in ("ERROR", "CRITICAL")]


def _describe(header: str, rest: list[str]) -> str:
    """One line per error: the wrapped header message plus the final
    exception line of its traceback. Deliberately nothing else -- the
    surrounding log carries conversation text (lk.pii.*), which has no
    business in a monitoring alert."""
    words = [header]
    for line in rest:
        text = line.strip()
        if not text or text.startswith(("{", '"', "Traceback")) or not line.startswith(" " * 20):
            break
        words.append(text)
    message = " ".join(" ".join(words).split())
    exc = ""
    for line in rest:
        m = _EXC_RE.match(line.strip())
        if m and not line.startswith(" "):
            exc = f"{m.group(1).rsplit('.', 1)[-1]}: {m.group(2)}".strip(": ")
    message = re.sub(r"lk\.pii\S*", "", message)
    exc = re.sub(r"lk\.pii\S*", "", exc)
    return (message + (f" — {exc}" if exc else ""))[:300]


def _check_recent_errors() -> str | None:
    log_path = config.LOGS_DIR / "app.log"
    if not log_path.exists():
        return None
    found: list[str] = []
    for header, rest in _error_records(_read_new_log_lines(log_path)):
        if any(marker in header or any(marker in line for line in rest) for marker in _SELF_HEALING):
            continue
        text = _describe(header, rest)
        if text not in found:
            found.append(text)
    if not found:
        return None
    more = f"\n…и ещё {len(found) - 3}" if len(found) > 3 else ""
    return "В логе Джарвиса новая ошибка:\n" + "\n".join(f"• {t}" for t in found[:3]) + more


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

    client = TelegramClient(
        tg_session.own_copy(config.TELEGRAM_MONITOR_SESSION_PATH), config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH
    )
    await tg_session.connect_with_retry(client)

    logger.info("proactive monitor running, checks every %.0fs, outbox every %.0fs",
                config.MONITOR_CHECK_INTERVAL_S, _OUTBOX_INTERVAL_S)

    last_checks = 0.0
    last_leads_poll = 0.0
    while True:
        owner_id = telegram_owner.load_owner_id()
        if owner_id is not None and await client.is_user_authorized():
            if site_leads.enabled() and time.time() - last_leads_poll >= config.SITE_LEADS_POLL_S:
                last_leads_poll = time.time()
                try:
                    await asyncio.to_thread(site_leads.poll)   # queues into the outbox drained just below
                except Exception:
                    logger.exception("site leads poll failed")
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
