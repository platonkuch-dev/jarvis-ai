"""Calendar: date parsing (Russian relative days, day-first, ISO) and deleting events."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest

import config
from tools import scheduling


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def events(tmp_path, monkeypatch):
    path = tmp_path / "events.json"
    monkeypatch.setattr(config, "EVENTS_FILE", path)
    monkeypatch.setattr(scheduling._events_store, "path", path)
    return path


def test_parse_dates():
    now = datetime.now()
    tomorrow = (now + timedelta(days=1)).date()
    p = scheduling._parse_datetime
    assert p("завтра в 15:00") == datetime.combine(tomorrow, datetime.min.time()).replace(hour=15)
    assert p("сегодня").date() == now.date()
    assert p("2026-10-02") == datetime(2026, 10, 2)                  # ISO is never read day-first
    assert p("2026-10-02 15:00") == datetime(2026, 10, 2, 15, 0)
    assert p("02.10") == datetime(now.year, 10, 2)                    # day.month, not a time
    assert p("2.10 в 18:30") == datetime(now.year, 10, 2, 18, 30)
    assert p("02.10.2026 09:15") == datetime(2026, 10, 2, 9, 15)
    assert abs((p("через 20 минут") - now).total_seconds() - 1200) < 5
    t = p("в 23.50")
    assert (t.hour, t.minute) == (23, 50) and t > now - timedelta(seconds=1)
    assert p("абракадабра") is None


def test_delete_event_single_ambiguous_all(events):
    run(scheduling._create_event(title="Встреча с Егором", datetime_str="завтра в 15:00"))
    run(scheduling._create_event(title="Поиграть в CS", datetime_str="завтра в 21:00"))
    run(scheduling._create_event(title="Поиграть в CS", datetime_str="послезавтра в 21:00"))

    res = run(scheduling._delete_event(query="егор"))
    assert res["status"] == "ok" and "Егором" in res["message"]

    res = run(scheduling._delete_event(query="cs"))                   # two match: ask, delete nothing
    assert res["status"] == "ambiguous"
    assert len(json.loads(events.read_text(encoding="utf-8"))) == 2

    res = run(scheduling._delete_event(query="cs", date_str="завтра"))
    assert res["status"] == "ok"
    left = json.loads(events.read_text(encoding="utf-8"))
    assert len(left) == 1 and left[0]["start"].startswith((datetime.now() + timedelta(days=2)).date().isoformat())

    assert run(scheduling._delete_event(query="нет такого"))["status"] == "not_found"
    run(scheduling._create_event(title="Ещё CS", datetime_str="послезавтра в 22:00"))
    res = run(scheduling._delete_event(date_str="послезавтра", delete_all=True))
    assert res["status"] == "ok" and json.loads(events.read_text(encoding="utf-8")) == []
