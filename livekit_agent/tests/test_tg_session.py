from __future__ import annotations

import asyncio
import sqlite3

import pytest

import config
import tg_session


def test_own_copy_bootstraps_once(tmp_path, monkeypatch):
    main = tmp_path / "jarvis_telegram"
    monkeypatch.setattr(config, "TELEGRAM_SESSION_PATH", str(main))
    (tmp_path / "jarvis_telegram.session").write_bytes(b"auth-key-1")

    path = tg_session.own_copy(str(main) + "_worker_console")
    assert (tmp_path / "jarvis_telegram_worker_console.session").read_bytes() == b"auth-key-1"

    # An existing copy is the process's own state (entity cache, pts) -- never overwritten.
    (tmp_path / "jarvis_telegram.session").write_bytes(b"auth-key-2")
    tg_session.own_copy(path)
    assert (tmp_path / "jarvis_telegram_worker_console.session").read_bytes() == b"auth-key-1"


class _Client:
    def __init__(self, failures: int, error: str = "database is locked") -> None:
        self.failures, self.error, self.calls = failures, error, 0

    async def connect(self) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise sqlite3.OperationalError(self.error)


def test_connect_retries_a_transient_lock(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(tg_session.asyncio, "sleep", no_sleep)
    client = _Client(failures=2)
    asyncio.run(tg_session.connect_with_retry(client))
    assert client.calls == 3


def test_connect_does_not_swallow_other_sqlite_errors():
    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(tg_session.connect_with_retry(_Client(failures=1, error="no such table: sessions")))
