"""Second monitor, day plan page, holographic core face. No network, no real screen."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import config
import fast_path
import screens


class _FakePG:
    Size = staticmethod(lambda w, h: (w, h))
    Point = staticmethod(lambda x, y: (x, y))

    def __init__(self):
        self.calls = []
        self.PAUSE = 0

    def __getattr__(self, name):
        def rec(*a, **kw):
            self.calls.append((name, a))
            return (-1820, 556) if name == "position" else None
        return rec


def test_monitor_proxy_shifts_coordinates(monkeypatch):
    mon = screens.Monitor(2, -1920, 456, 1920, 1200, False)
    proxy = screens.MonitorPyAutoGUI.__new__(screens.MonitorPyAutoGUI)
    fake = _FakePG()
    object.__setattr__(proxy, "_pg", fake)
    object.__setattr__(proxy, "monitor", mon)
    object.__setattr__(proxy, "_dx", mon.left)
    object.__setattr__(proxy, "_dy", mon.top)
    assert proxy.size() == (1920, 1200)
    proxy.click(100, 50, button="left")
    proxy.dragTo(10, 20)
    proxy.scroll(-3, 5, 5)
    assert fake.calls[0] == ("click", (-1820, 506))
    assert fake.calls[1] == ("dragTo", (-1910, 476))
    assert fake.calls[2] == ("scroll", (-3, -1915, 461))
    assert proxy.position() == (100, 100)
    proxy.PAUSE = 0.05                     # settings go to the real module
    assert fake.PAUSE == 0.05


def test_monitor_numbering(monkeypatch):
    monkeypatch.setattr(screens, "monitors", lambda: [screens.Monitor(1, 0, 0, 3840, 2160, True),
                                                      screens.Monitor(2, -1920, 456, 1920, 1200, False)])
    assert screens.get(2).describe() == "монитор 2 (слева, 1920×1200)"
    with pytest.raises(ValueError):
        screens.get(3)
    import pyautogui

    assert screens.pyautogui_for(1) is pyautogui


def test_fast_path_day_plan():
    assert fast_path.parse("открой план на день на втором мониторе").args == {"monitor": 2}
    assert fast_path.parse("покажи расписание").args == {"monitor": 2}
    assert fast_path.parse("открой план дня на основном экране").args == {"monitor": 1}
    assert fast_path.parse("закрой план").tool == "close_day_plan"
    assert fast_path.parse("закрой панель").tool == "close_hud_panel"
    assert fast_path.parse("перейди на список дел").tool == "show_day_plan"
    assert fast_path.parse("закрой список дел").tool == "close_day_plan"
    assert fast_path.parse("выключи микрофон").args == {"on": False}
    assert fast_path.parse("выключи звук").tool == "system_control"          # sound, not the mic
    assert fast_path.parse("выведи себя на второй экран").tool == "open_hud_panel"


def test_day_plan_collects(tmp_path, monkeypatch):
    import dayplan_data

    now = datetime.now().replace(second=0, microsecond=0)
    ev = tmp_path / "events.json"
    ev.write_text(json.dumps([
        {"title": "Сегодня", "start": now.replace(hour=15, minute=30).isoformat(), "duration_minutes": 45},
        {"title": "Завтра", "start": (now + timedelta(days=1)).isoformat(), "duration_minutes": 30},
    ]), encoding="utf-8")
    todos = tmp_path / "todos.json"
    todos.write_text(json.dumps([{"text": "a", "done": False}, {"text": "b", "done": True}]), encoding="utf-8")
    monkeypatch.setattr(config, "EVENTS_FILE", ev)
    monkeypatch.setattr(config, "TODOS_FILE", todos)
    monkeypatch.setattr(config, "MEMORY_FILE", tmp_path / "memory.json")
    monkeypatch.setattr(config, "HOME_CITY", "")
    data = dayplan_data.collect()
    assert data["events"] == [{"title": "Сегодня", "start": 930, "duration": 45}]
    assert data["todos"] == ["a"] and data["weather"] is None


def test_panel_server_routes(monkeypatch):
    import http.client

    import hud_bridge
    import hud_panel

    monkeypatch.setattr(hud_bridge, "read_state", lambda: {"status": "speaking", "lines": [{"role": "assistant", "text": "Привет"}], "updated_at": 1.0})
    monkeypatch.setattr(hud_panel, "PORT", 48127)    # never the live Jarvis's own panel server
    monkeypatch.setattr(hud_panel, "_server", None)
    monkeypatch.setattr(hud_panel, "STATE_FILE", config.DATA_DIR / "hud_panel_state.json")
    hud_panel.set_plan(True)
    assert hud_panel.serve(level_fn=lambda: (0.5, 0.1))
    conn = http.client.HTTPConnection("127.0.0.1", hud_panel.PORT, timeout=3)
    conn.request("GET", "/", headers={"Host": f"127.0.0.1:{hud_panel.PORT}"})
    page = conn.getresponse().read().decode("utf-8")
    assert "EventSource" in page and "/data.json" in page
    conn.request("GET", "/", headers={"Host": "evil.example:80"})
    assert conn.getresponse().status == 403
    conn.request("GET", "/events", headers={"Host": f"127.0.0.1:{hud_panel.PORT}"})
    resp = conn.getresponse()
    first = resp.fp.readline().decode("utf-8")
    msg = json.loads(first.removeprefix("data: "))
    assert msg["s"] == "speaking" and msg["l"] == 0.5 and msg["lines"][0]["text"] == "Привет"
    assert msg["p"] is True
    conn.close()


@pytest.fixture(scope="module")
def app():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_core_face_cycle(app, monkeypatch):
    hud_core = pytest.importorskip("hud_core")
    clock = [1000.0]
    monkeypatch.setattr(hud_core.time, "monotonic", lambda: clock[0])
    core = hud_core.CorePanel()
    core._rx.ok = False

    def run(seconds, level=0.0):
        for _ in range(int(seconds / 0.016)):
            clock[0] += 0.016
            core._level = level
            core._step()
            core.grab()

    assert core._mode == "ember" and core._awake == 0.0
    core.set_status("listening")
    core.play_assembly()
    run(0.8)
    assert core._mode == "assemble" and 0 < core._progress < 1
    run(1.2)
    assert core._mode == "shown" and core._awake == 1.0
    core.set_status("speaking")
    core._last_audio = clock[0]
    run(0.5, level=0.8)
    assert core._energy > 0.3
    core.set_status("sleeping")
    run(3.0)
    assert core._mode == "ember" and core._awake == 0.0
    core.set_status("listening")               # waking replays the assembly
    assert core._mode == "assemble"


def test_panel_buttons(monkeypatch):
    import http.client

    import hud_panel
    import mic_control

    monkeypatch.setattr(mic_control, "STATE_FILE", config.DATA_DIR / "mic_state.json")
    monkeypatch.setattr(hud_panel, "STATE_FILE", config.DATA_DIR / "hud_panel_state.json")
    monkeypatch.setattr(hud_panel, "PORT", 48128)
    monkeypatch.setattr(hud_panel, "_server", None)
    assert hud_panel.serve()
    host = {"Host": "127.0.0.1:48128", "Content-Type": "application/json"}
    conn = http.client.HTTPConnection("127.0.0.1", 48128, timeout=3)

    conn.request("POST", "/control", body=json.dumps({"mic": False}), headers=host)   # no X-Jarvis: a foreign page
    assert conn.getresponse().status == 403 and not mic_control.is_muted()

    conn.request("POST", "/control", body=json.dumps({"mic": False, "plan": True}), headers={**host, "X-Jarvis": "1"})
    assert conn.getresponse().status == 200
    assert mic_control.is_muted() and hud_panel.plan_visible()
    conn.request("POST", "/control", body=json.dumps({"mic": True, "plan": False}), headers={**host, "X-Jarvis": "1"})
    conn.getresponse().read()
    assert not mic_control.is_muted() and not hud_panel.plan_visible()
    conn.close()


def test_sleep_wake_follows_mic_switch(monkeypatch):
    import mic_control
    import sleep_wake

    monkeypatch.setattr(mic_control, "STATE_FILE", config.DATA_DIR / "mic_state.json")
    monkeypatch.setattr(sleep_wake, "MIC_POLL_S", 0.01)
    said = []

    async def fake_say(text):
        said.append(text)

    monkeypatch.setattr(sleep_wake.runtime, "say", fake_say)
    monkeypatch.setattr(sleep_wake.hud_bridge, "write_state", lambda *a: None)

    class Input:
        enabled = True

        def set_audio_enabled(self, on):
            self.enabled = on

    class Session:
        input = Input()

    session = Session()
    ctl = sleep_wake.SleepWakeController(session, [])

    async def scenario():
        task = asyncio.create_task(ctl._mic_loop())
        mic_control.set_muted(True)
        await asyncio.sleep(0.1)
        assert ctl.muted and not session.input.enabled
        await ctl._sleep()                      # asleep while muted: no wake word
        mic_control.set_muted(False)            # the button / F10: unmutes and wakes
        await asyncio.sleep(0.1)
        assert not ctl.muted and not ctl.asleep and session.input.enabled
        task.cancel()

    asyncio.run(scenario())
    assert said[0] == "Микрофон выключен." and said[-1] == "Слушаю."


def test_mic_hotkey_toggles(monkeypatch):
    import mic_control
    import sleep_wake

    monkeypatch.setattr(mic_control, "STATE_FILE", config.DATA_DIR / "mic_state.json")
    ctl = sleep_wake.SleepWakeController(object(), [])
    mic_control.set_muted(False)
    ctl._on_mic_hotkey()
    assert mic_control.is_muted()
    ctl._on_mic_hotkey()
    assert not mic_control.is_muted()
    assert config.MIC_HOTKEY == "f9"
