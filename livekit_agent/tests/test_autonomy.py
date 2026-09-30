"""Tasks, triggers, approvals, notifications, budget, reminders, Telegram
pairing -- everything that lets Jarvis act without being asked, tested
offline (no model, no Telegram, no real screen)."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace as NS

import approvals
import notify
import policy
import telegram_owner
import usage
from tools import runtime, scheduling, tasks, triggers

# --- notify outbox ----------------------------------------------------------


def test_outbox_roundtrip_and_requeue():
    notify.notify_owner("one")
    notify.notify_owner("two")
    taken = notify.drain()
    assert [e["text"] for e in taken] == ["one", "two"]
    assert notify.drain() == []
    notify.requeue(taken[1:])
    assert [e["text"] for e in notify.drain()] == ["two"]


# --- Telegram pairing -------------------------------------------------------


def test_owner_needs_the_pairing_code():
    code = telegram_owner.pairing_code()
    assert len(code) == 6
    assert not telegram_owner.try_claim(111, "stranger", "привет")
    assert telegram_owner.load_owner_id() is None
    assert telegram_owner.try_claim(222, "Owner", f"мой код {code[:3]} {code[3:]}")
    assert telegram_owner.load_owner_id() == 222
    # The code is single-use and a claimed account can't be re-claimed.
    assert not telegram_owner.try_claim(333, "late", code)
    assert telegram_owner.load_owner_id() == 222


def test_reset_owner_issues_new_code():
    telegram_owner.try_claim(1, "a", telegram_owner.pairing_code())
    new_code = telegram_owner.reset_owner()
    assert telegram_owner.load_owner_id() is None
    assert telegram_owner.try_claim(2, "b", new_code)


# --- approvals --------------------------------------------------------------


async def test_approval_answer_by_code_and_plain_yes():
    first = approvals.create("отправить сообщение", 60)
    assert await approvals.handle_owner_reply("привет, как дела") is None
    reply = await approvals.handle_owner_reply("да")  # the only open question
    assert reply.startswith("Разрешено")
    assert approvals.status(first) == "approved"

    a = approvals.create("закрыть хром", 60)
    b = approvals.create("открыть сайт", 60)
    assert await approvals.handle_owner_reply("да") is None  # ambiguous with 2 open
    await approvals.handle_owner_reply(f"нет {a}")
    await approvals.handle_owner_reply(f"да {b}")
    assert approvals.status(a) == "denied" and approvals.status(b) == "approved"


async def test_approval_timeout_is_no(outbox):
    assert await approvals.request_approval("что-то", timeout_s=0.05) is False
    assert any("Нужно ваше решение" in t for t in outbox.texts())


# --- policy -----------------------------------------------------------------


def test_policy_levels():
    assert policy.classify("get_weather") == policy.SAFE
    assert policy.classify("system_control", {"action": "volume"}) == policy.SAFE
    assert policy.classify("system_control", {"action": "sleep"}) == policy.BLOCK
    assert policy.classify("file_manager", {"action": "delete"}) == policy.CONFIRM
    assert policy.classify("file_manager", {"action": "read"}) == policy.SAFE
    assert policy.classify("send_telegram_message") == policy.CONFIRM
    assert policy.classify("start_task") == policy.BLOCK
    assert policy.classify("some_future_tool") == policy.CONFIRM


# --- budget -----------------------------------------------------------------


def test_cost_and_budget(outbox):
    assert usage.price_for("claude-opus-5-5") == (4.0, 20.0)
    assert usage.price_for("claude-haiku-4-5-20251001") == (1.0, 5.0)
    one_m_in = usage.cost_usd("claude-haiku-4-5", input_tokens=1_000_000)
    assert abs(one_m_in - 1.0) < 1e-9
    assert abs(usage.cost_usd("claude-haiku-4-5", cache_read=1_000_000) - 0.1) < 1e-9

    assert usage.check_budget("x") is None
    usage.record("claude-sonnet-5", output_tokens=400_000, source="test")  # $4 > $3
    assert usage.budget_left() < 0
    assert "лимит" in usage.check_budget("работу с экраном")
    usage.check_budget("ещё раз")
    assert len([t for t in outbox.texts() if "лимит" in t]) == 1  # notified once per day


# --- reminders --------------------------------------------------------------


async def test_reminders_survive_and_fire_late():
    now = datetime.now()
    await scheduling._add_reminder("позвонить", now - timedelta(minutes=30), "reminder")
    await scheduling._add_reminder("потом", now + timedelta(hours=1), "reminder")
    due = await scheduling.take_due_reminders(now)
    assert [r["text"] for r in due] == ["позвонить"]
    assert "Пропущенное" in scheduling._announcement(due[0], now)
    left = await scheduling._reminders_store.load()
    assert [r["text"] for r in left] == ["потом"]


async def test_timer_is_persisted():
    await scheduling._set_timer(minutes=0.01)
    due = await scheduling.take_due_reminders(datetime.now() + timedelta(seconds=5))
    assert due and due[0]["kind"] == "timer"
    assert scheduling._announcement(due[0], datetime.now()).startswith("Таймер на 0.01 минут истёк")


# --- triggers ---------------------------------------------------------------


def test_parse_when():
    assert triggers.parse_when("daily", "9:05 будни") == {"time": "09:05", "days": [0, 1, 2, 3, 4]}
    assert isinstance(triggers.parse_when("daily", "утром"), str)
    assert triggers.parse_when("interval", "3") != {"minutes": 3}  # too frequent
    assert triggers.parse_when("app_start", "Discord.exe") == {"process": "discord"}


def test_daily_due_window():
    spec = {"time": "09:00", "days": list(range(7))}
    at = datetime(2026, 9, 24, 9, 10)
    assert triggers.daily_due(spec, 0, at)
    assert not triggers.daily_due(spec, at.timestamp(), at)            # already fired today
    assert not triggers.daily_due(spec, 0, datetime(2026, 9, 24, 8, 59))
    assert not triggers.daily_due(spec, 0, datetime(2026, 9, 24, 11, 0))  # missed by > 1h


def test_watcher_edges(tmp_path):
    w = triggers._Watcher()
    app = {"id": "a", "when": "app_start", "spec": {"process": "discord"}}
    assert w.check(app, datetime.now(), {"chrome"}) is None       # baseline
    assert w.check(app, datetime.now(), {"chrome", "discord"}) == "discord"
    assert w.check(app, datetime.now(), {"chrome", "discord"}) is None

    folder = tmp_path / "in"
    folder.mkdir()
    new = {"id": "f", "when": "file_new", "spec": {"folder": str(folder), "pattern": "*.pdf"}}
    assert w.check(new, datetime.now(), None) is None
    (folder / "a.txt").write_text("x")
    (folder / "b.crdownload").write_text("x")
    assert w.check(new, datetime.now(), None) is None
    (folder / "Report.PDF").write_text("x")
    assert w.check(new, datetime.now(), None).endswith("Report.PDF")

    start = {"id": "s", "when": "startup", "spec": {}}
    assert w.check(start, datetime.now(), None) == "запуск"
    assert w.check(start, datetime.now(), None) is None


async def test_create_trigger_needs_confirmation_then_fires_task():
    r = await triggers._create_trigger(name="pdf", when="file_new", when_value="Z:/нет/такой/папки",
                                       action="task", action_value="разбери {detail}")
    assert r["status"] == "error"
    import tempfile

    folder = tempfile.mkdtemp()
    r = await triggers._create_trigger(name="pdf", when="file_new", when_value=folder,
                                       action="task", action_value="разбери {detail}")
    assert r["status"] == "needs_confirmation"
    assert await triggers._store.load() == []
    r = await triggers._create_trigger(name="pdf", when="file_new", when_value=folder,
                                       action="task", action_value="разбери {detail}", confirm=True)
    assert r["status"] == "ok"
    trig = (await triggers._store.load())[0]
    await triggers.fire(trig, "C:/x/a.pdf")
    queued = await tasks._store.load()
    assert queued[0]["goal"].startswith("разбери C:/x/a.pdf") and queued[0]["source"] == "trigger"
    assert (await triggers._store.load())[0]["fire_count"] == 1


# --- tasks ------------------------------------------------------------------


async def test_task_lifecycle_and_recovery():
    r = await tasks._start_task(goal="проверь погоду")
    tid = r["task_id"]
    claimed = await tasks._claim_next()
    assert claimed["id"] == tid and claimed["status"] == "running"
    assert await tasks._claim_next() is None
    await tasks._recover()                      # simulated crash mid-run
    again = await tasks._claim_next()
    assert again["attempts"] == 2
    await tasks._recover()                      # second crash: give up
    final = (await tasks._store.load())[0]
    assert final["status"] == "failed"


async def test_task_gate(monkeypatch):
    task = {"id": "t1", "goal": "g", "trusted": False}
    await tasks.enqueue("g")
    gate = tasks._make_gate(task)
    assert await gate("get_weather", {}) is None
    assert "запрещено" in await gate("start_task", {})
    # No Telegram owner -> a confirm-level step is refused, not silently run.
    assert "Telegram" in await gate("send_telegram_message", {"recipient": "x", "message": "y"})

    telegram_owner.try_claim(5, "o", telegram_owner.pairing_code())

    async def deny(question, timeout_s=None):
        return False

    async def allow(question, timeout_s=None):
        return True

    monkeypatch.setattr(approvals, "request_approval", deny)
    monkeypatch.setattr(tasks, "_is_cancelled", lambda tid: False)
    assert "не разрешил" in await gate("close_application", {"name": "chrome"})
    monkeypatch.setattr(approvals, "request_approval", allow)
    assert await gate("close_application", {"name": "chrome"}) is None
    assert await tasks._make_gate({**task, "trusted": True})("use_computer", {"task": "x"}) is None


async def test_run_task_reports(monkeypatch, outbox):
    import agent_loop

    async def fake_run(**kwargs):
        assert "start_task" not in {t["name"] for t in kwargs["tools_param"]}
        assert "Задача: собери отчёт" in kwargs["user_text"]
        return "Отчёт собран и лежит на рабочем столе.", []

    monkeypatch.setattr(agent_loop, "run", fake_run)
    monkeypatch.setattr(agent_loop, "tool_schemas", lambda exclude=frozenset(): [
        {"name": n} for n in ("get_weather", "start_task") if n not in exclude])
    await tasks._start_task(goal="собери отчёт")
    task = await tasks._claim_next()
    status, text = await tasks.run_task(task)
    assert status == "done"
    await tasks._report(task, status, text)            # no voice session -> Telegram
    assert any("Отчёт собран" in t for t in outbox.texts())


# --- agent loop -------------------------------------------------------------


async def test_agent_loop_runs_tools_through_gate(monkeypatch):
    import agent_loop
    import config
    from tools.registry import IMPL_REGISTRY

    monkeypatch.setattr(config, "SUBSCRIPTION_MODE", False)  # this covers the direct-API loop
    calls = []

    async def fake_weather(*, location):
        calls.append(location)
        return {"status": "ok", "message": "+20"}

    monkeypatch.setitem(IMPL_REGISTRY, "get_weather", fake_weather)

    replies = [
        NS(content=[NS(type="tool_use", id="1", name="get_weather", input={"location": "Киев"},
                       model_dump=lambda: {"type": "tool_use"}),
                    NS(type="tool_use", id="2", name="send_telegram_message", input={"recipient": "a", "message": "b"},
                       model_dump=lambda: {"type": "tool_use"})],
           usage=NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)),
        NS(content=[NS(type="text", text="Там +20.", model_dump=lambda: {"type": "text"})],
           usage=NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)),
    ]
    sent = []

    class FakeMessages:
        async def create(self, **kwargs):
            sent.append(kwargs["messages"][-1])
            return replies.pop(0)

    monkeypatch.setattr(agent_loop, "client", lambda: NS(messages=FakeMessages()))

    async def gate(tool, args):
        return "нельзя" if tool == "send_telegram_message" else None

    text, _ = await agent_loop.run(system_text="s", tools_param=[], history=[], user_text="погода?", gate=gate)
    assert text == "Там +20." and calls == ["Киев"]
    results = sent[1]["content"]
    assert results[1]["is_error"] is True and "нельзя" in results[1]["content"]
    assert usage.spent_today() > 0


def test_user_presence(monkeypatch):
    monkeypatch.setattr(runtime, "_active_session", None)
    assert runtime.user_present() is False
    monkeypatch.setattr(runtime, "_active_session", object())
    monkeypatch.setattr(runtime, "_presence_probe", lambda: False)
    assert runtime.user_present() is False
    monkeypatch.setattr(runtime, "_presence_probe", None)
    assert runtime.user_present() is True
