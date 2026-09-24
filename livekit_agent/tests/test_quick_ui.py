"""quick_ui: free UI Automation first, use_computer as the automatic fallback."""

from __future__ import annotations

import pytest

import config
from tools import quick_ui
from tools.registry import IMPL_REGISTRY


@pytest.fixture(autouse=True)
def fakes(tmp_path, monkeypatch):
    monkeypatch.setattr(quick_ui, "STATS_FILE", tmp_path / "quick_ui_stats.json")
    monkeypatch.setattr(config, "SYSTEM", "Windows")
    calls = {"uia": [], "screen": []}
    outcome = {"ok": True}

    async def fake_uia(app, action, target, text):
        calls["uia"].append((app, action, target))
        return outcome["ok"], "Clicked." if outcome["ok"] else "not found"

    async def fake_screen(*, task, max_steps=None):
        calls["screen"].append(task)
        return {"status": "ok", "message": "Сделал глазами."}

    monkeypatch.setattr(quick_ui, "_try_uia", fake_uia)
    monkeypatch.setitem(IMPL_REGISTRY, "use_computer", fake_screen)
    return calls, outcome


async def test_uia_success_never_touches_the_screen_agent(fakes):
    calls, _ = fakes
    r = await quick_ui._quick_ui(app="Блокнот", action="click", target="Сохранить")
    assert r["method"] == "ui_automation" and calls["screen"] == []


async def test_uia_failure_falls_back_automatically(fakes):
    calls, outcome = fakes
    outcome["ok"] = False
    r = await quick_ui._quick_ui(app="Параметры", action="type", target="Поиск", text="bluetooth")
    assert r["method"] == "use_computer" and r["message"] == "Сделал глазами."
    assert calls["screen"] == ["В окне приложения «Параметры» введи в поле «Поиск» текст: bluetooth"]


@pytest.mark.parametrize("app", ["After Effects", "VS Code", "code.exe", "Discord", "Google Chrome", "Photoshop"])
async def test_blind_apps_go_straight_to_screen(fakes, app):
    calls, _ = fakes
    r = await quick_ui._quick_ui(app=app, action="click", target="Файл")
    assert r["method"] == "use_computer" and calls["uia"] == []


@pytest.mark.parametrize("target", ["Удалить", "Отправить", "Оплатить заказ", "Delete", "Sign out"])
async def test_risky_clicks_go_straight_to_screen(fakes, target):
    calls, _ = fakes
    await quick_ui._quick_ui(app="Блокнот", action="click", target=target)
    assert calls["uia"] == []


def test_not_risky_lookalikes():
    for target in ("Мастер установки", "Postman", "Reorder", "Сохранить"):
        assert quick_ui.route("Блокнот", "click", target) is None, target
    assert quick_ui.route("Word", "click", "Сохранить") is None  # "word" isn't "code"


async def test_app_demoted_after_repeated_failures(fakes):
    calls, outcome = fakes
    outcome["ok"] = False
    for _ in range(quick_ui._MAX_FAILS):
        await quick_ui._quick_ui(app="OldApp", action="click", target="OK")
    assert len(calls["uia"]) == quick_ui._MAX_FAILS
    await quick_ui._quick_ui(app="OldApp", action="click", target="OK")
    assert len(calls["uia"]) == quick_ui._MAX_FAILS  # skipped UIA this time
    assert "часто ошибался" in quick_ui.route("oldapp.exe", "click", "OK")


def test_policy_for_background_tasks():
    import policy

    assert policy.classify("quick_ui", {"action": "read"}) == policy.SAFE
    assert policy.classify("quick_ui", {"action": "click"}) == policy.CONFIRM
