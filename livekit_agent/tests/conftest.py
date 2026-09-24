"""Shared fixtures: every test gets its own empty data directory, so nothing
here ever reads or writes the real data/ (memory, Telegram owner, tasks...)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import tools  # noqa: E402,F401  (registers every tool)


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    import approvals
    import notify
    import telegram_owner
    from tools import scheduling, tasks, triggers

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    for name in ("TASKS_FILE", "TRIGGERS_FILE", "USAGE_FILE", "REMINDERS_FILE", "TELEGRAM_OWNER_FILE"):
        monkeypatch.setattr(config, name, tmp_path / Path(getattr(config, name)).name)
    monkeypatch.setattr(notify, "OUTBOX_FILE", tmp_path / "notify_outbox.jsonl")
    monkeypatch.setattr(approvals, "APPROVALS_FILE", tmp_path / "approvals.json")
    monkeypatch.setattr(telegram_owner, "PAIR_CODE_FILE", tmp_path / "telegram_pair_code.txt")
    monkeypatch.setattr(tasks._store, "path", config.TASKS_FILE)
    monkeypatch.setattr(triggers._store, "path", config.TRIGGERS_FILE)
    monkeypatch.setattr(scheduling._reminders_store, "path", config.REMINDERS_FILE)
    monkeypatch.setattr(config, "DAILY_BUDGET_USD", 3.0)
    monkeypatch.setattr(config, "TOOL_LOG_FILE", tmp_path / "tool_calls.log")
    import tools._logging as tool_logging

    monkeypatch.setattr(tool_logging, "TOOL_LOG_FILE", tmp_path / "tool_calls.log")
    monkeypatch.setattr(tool_logging, "_note_pattern_learning", lambda name: None)
    yield tmp_path


@pytest.fixture
def outbox():
    """Everything queued for the owner's Telegram during the test."""
    import notify

    class _Outbox:
        def texts(self) -> list[str]:
            return [e["text"] for e in notify.drain()]

    return _Outbox()
