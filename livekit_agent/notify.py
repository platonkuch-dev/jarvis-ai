"""Cross-process "tell the owner" outbox.

Any process (the voice worker, the tray supervisor, the task runner) can call
`notify_owner()` without holding a Telegram connection of its own: the line
lands in a small JSONL file, and proactive_monitor.py -- which already keeps a
Telegram session and knows the owner's id -- drains it every few seconds and
delivers each entry. No LLM involved, so it costs nothing.

If Telegram isn't set up the outbox simply isn't drained; it's capped at
_MAX_PENDING lines so it can never grow without bound.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import config

OUTBOX_FILE = config.DATA_DIR / "notify_outbox.jsonl"
_MAX_PENDING = 200


def notify_owner(text: str, *, kind: str = "info") -> None:
    """Queue `text` for delivery to the owner's Telegram. Never raises."""
    entry = {"id": uuid.uuid4().hex[:10], "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "text": text}
    try:
        OUTBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(OUTBOX_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _trim()
    except OSError:
        pass


def _trim() -> None:
    try:
        if OUTBOX_FILE.stat().st_size < 64 * 1024:
            return
        lines = OUTBOX_FILE.read_text(encoding="utf-8").splitlines()
        if len(lines) > _MAX_PENDING:
            OUTBOX_FILE.write_text("\n".join(lines[-_MAX_PENDING:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def requeue(entries: list[dict]) -> None:
    """Put back entries that failed to send (e.g. no network) for the next drain."""
    if not entries:
        return
    try:
        with open(OUTBOX_FILE, "a", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _trim()
    except OSError:
        pass


def drain(path: Path | None = None) -> list[dict]:
    """Atomically take every pending entry. Renaming first means a writer
    appending at the same moment either lands in the old file (taken now) or
    creates a fresh one (taken next time) -- nothing is lost or sent twice."""
    path = path or OUTBOX_FILE
    if not path.exists():
        return []
    taking = path.with_suffix(".sending")
    try:
        os.replace(path, taking)
    except OSError:
        return []  # file briefly locked by a writer -- next tick
    entries: list[dict] = []
    try:
        for line in taking.read_text(encoding="utf-8").splitlines():
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
    finally:
        try:
            taking.unlink()
        except OSError:
            pass
    return entries
