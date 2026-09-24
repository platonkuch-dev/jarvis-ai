from __future__ import annotations

import pytest

import fast_path


@pytest.mark.parametrize("text, tool, args", [
    ("Громкость 30", "system_control", {"action": "volume", "value": 30}),
    ("Джарвис, сделай громкость на 50 процентов.", "system_control", {"action": "volume", "value": 50}),
    ("звук на двадцать", "system_control", {"action": "volume", "value": 20}),
    ("яркость 70", "system_control", {"action": "brightness", "value": 70}),
    ("громче", "volume_step", {"delta": 10}),
    ("сделай потише", "volume_step", {"delta": -10}),
    ("выключи звук", "system_control", {"action": "volume", "value": 0}),
    ("Пауза.", "media_control", {"action": "pause"}),
    ("следующий трек", "media_control", {"action": "next"}),
    ("включи предыдущую песню", "media_control", {"action": "prev"}),
    ("поставь таймер на 5 минут", "set_timer", {"minutes": 5}),
    ("таймер на пять минут", "set_timer", {"minutes": 5}),
    ("таймер на 90 секунд", "set_timer", {"minutes": 1.5}),
    ("таймер на полчаса", "set_timer", {"minutes": 30}),
    ("таймер на час", "set_timer", {"minutes": 60}),
    ("заблокируй компьютер", "system_control", {"action": "lock"}),
    ("открой телеграм", "open_application", {"name": "telegram"}),
    ("запусти хром пожалуйста", "open_application", {"name": "chrome"}),
    ("открой discord", "open_application", {"name": "discord"}),
    ("закрой спотифай", "close_application", {"name": "spotify"}),
])
def test_recognised(text, tool, args):
    intent = fast_path.parse(text)
    assert intent is not None, text
    assert intent.tool == tool
    assert intent.args == args


@pytest.mark.parametrize("text", [
    "открой файл отчёт за март",          # a file, not an app
    "открой сайт ютуба",                   # unknown Russian name -> LLM translates
    "закрой проводник",                    # protected
    "закрой explorer",                     # protected
    "громкость на максимум и включи музыку",
    "что дальше",
    "поставь напоминание на завтра",
    "стоп",                                # only meaningful while use_computer runs
    "громкость 250",
    "расскажи анекдот про таймер на пять минут",
    "",
])
def test_left_to_llm(text):
    assert fast_path.parse(text) is None


def test_stop_only_while_screen_agent_runs():
    assert fast_path.parse("стоп", computer_use_running=True).tool == "stop_computer_use"


def test_local_answers_need_no_tool():
    assert fast_path.parse("который час").tool is None
    assert fast_path.parse("какое сегодня число").answer.startswith("Сегодня ")


async def test_execute_falls_back_to_llm_on_crash(monkeypatch):
    from tools.registry import IMPL_REGISTRY

    async def boom(**kwargs):
        raise RuntimeError("no audio device")

    monkeypatch.setitem(IMPL_REGISTRY, "media_control", boom)
    handled, _ = await fast_path.execute(fast_path.Intent("media_control", {"action": "next"}))
    assert handled is False
