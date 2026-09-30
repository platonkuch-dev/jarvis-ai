"""Skills added after comparing with other Jarvis projects: news/briefing,
exchange rates, music, smart home, e-mail, timers, power actions. No network."""

from __future__ import annotations

import asyncio

import pytest

import config
import fast_path
import policy
from tools import briefing, scheduling, smart_home, triggers
from tools.registry import IMPL_REGISTRY

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Первая &amp; новость</title><link>https://a/1</link><description>&lt;p&gt;Текст&lt;/p&gt;</description></item>
<item><title>Вторая</title><link>https://a/2</link></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Атом</title><link href="https://b/1"/><summary>Кратко</summary></entry></feed>"""


def run(coro):
    return asyncio.run(coro)


def test_parse_rss_and_atom():
    items = briefing.parse_rss(RSS, 5)
    assert [i["title"] for i in items] == ["Первая & новость", "Вторая"]
    assert items[0]["summary"] == "Текст" and items[0]["link"] == "https://a/1"
    atom = briefing.parse_rss(ATOM, 5)
    assert atom == [{"title": "Атом", "link": "https://b/1", "summary": "Кратко"}]


def test_parse_feeds():
    assert briefing.parse_feeds("Хабр=https://habr.com/rss, https://x.org/feed") == [
        ("Хабр", "https://habr.com/rss"), ("x.org", "https://x.org/feed")]


def test_news_topic_filter_and_speech(monkeypatch):
    monkeypatch.setattr(config, "NEWS_FEEDS", "A=https://a")

    async def fake_fetch(client, name, url, limit):
        items = briefing.parse_rss(RSS, limit)
        for i in items:
            i["source"] = name
        return items

    monkeypatch.setattr(briefing, "_fetch_feed", fake_fetch)
    res = run(briefing._get_news(topic="вторая"))
    assert res["status"] == "ok" and [i["title"] for i in res["items"]] == ["Вторая"]
    assert res["speech"] == "Главные новости. Вторая."
    assert "https://a/2" in res["message"]


def test_briefing_without_network(monkeypatch):
    async def fake(**kw):
        return {"status": "ok", "message": "Тепло.", "events": [], "items": [{"title": "Н1"}]}

    for name in ("get_weather", "get_schedule", "get_news", "get_system_status"):
        monkeypatch.setitem(IMPL_REGISTRY, name, fake)
    res = run(briefing._morning_briefing(city="Днепр"))
    assert "Тепло." in res["message"] and "Н1" in res["message"]
    assert "Событий в календаре на сегодня нет" in res["message"]


def test_fast_path_new_intents():
    assert fast_path.parse("какие новости").tool == "get_news"
    assert fast_path.parse("утренняя сводка").tool == "morning_briefing"
    assert fast_path.parse("курс доллара").args == {"base": "USD", "target": "RUB"}
    assert fast_path.parse("курс евро к гривне").args == {"base": "EUR", "target": "UAH"}
    assert fast_path.parse("курс биткоина").args["base"] == "BTC"
    assert fast_path.parse("отмени таймер").args == {"query": "таймер"}
    assert fast_path.parse("сколько осталось").tool == "list_reminders"
    assert fast_path.parse("курс молодого бойца") is None


def test_named_timers_list_and_cancel():
    run(scheduling._set_timer(minutes=5, label="паста"))
    run(scheduling._set_timer(minutes=10, label="стирка"))
    run(scheduling._create_reminder(text="позвонить маме", datetime_str="2099-01-01 10:00"))
    listing = run(scheduling._list_reminders())["message"]
    assert "паста" in listing and "стирка" in listing and "позвонить маме" in listing
    assert "паста" in run(scheduling._cancel_reminder(query="паст"))["message"]
    assert "стирка" in run(scheduling._cancel_reminder(query="таймер"))["message"]
    assert run(scheduling._cancel_reminder(query="нет такого"))["status"] == "not_found"
    assert len(run(scheduling._list_reminders())["reminders"]) == 1


def test_shutdown_needs_confirmation_and_is_blocked_autonomously():
    res = run(IMPL_REGISTRY["system_control"](action="shutdown"))
    assert res["status"] == "needs_confirmation"
    assert policy.classify("system_control", {"action": "shutdown"}) == policy.BLOCK
    assert policy.classify("system_control", {"action": "mute"}) == policy.SAFE
    assert policy.classify("send_email", {}) == policy.CONFIRM
    assert policy.classify("smart_home", {"action": "state"}) == policy.SAFE
    assert policy.classify("smart_home", {"action": "on"}) == policy.CONFIRM


STATES = [
    {"entity_id": "light.kitchen", "state": "off", "attributes": {"friendly_name": "Свет на кухне"}},
    {"entity_id": "light.bedroom", "state": "on", "attributes": {"friendly_name": "Свет в спальне"}},
    {"entity_id": "climate.living", "state": "heat", "attributes": {"friendly_name": "Кондиционер"}},
    {"entity_id": "scene.movie", "state": "scening", "attributes": {"friendly_name": "Кино"}},
    {"entity_id": "sensor.temp", "state": "21", "attributes": {"friendly_name": "Температура кухня"}},
]


def test_smart_home_matching_and_services():
    assert smart_home.match_entity(STATES, "свет на кухне")["entity_id"] == "light.kitchen"
    assert smart_home.match_entity(STATES, "свет в спальне")["entity_id"] == "light.bedroom"
    assert smart_home.match_entity(STATES, "кино", ("scene",))["entity_id"] == "scene.movie"
    assert smart_home.match_entity(STATES, "гараж") is None
    assert smart_home._service_for("brightness", "light", "40") == ("light/turn_on", {"brightness_pct": 40})
    assert smart_home._service_for("temperature", "climate", "22")[0] == "climate/set_temperature"
    assert smart_home._service_for("on", "light", "")[0] == "homeassistant/turn_on"
    assert smart_home._service_for("brightness", "switch", "40")[0] is None


def test_unconfigured_integrations_explain_setup(monkeypatch):
    monkeypatch.setattr(config, "HOME_ASSISTANT_URL", "")
    monkeypatch.setattr(config, "EMAIL_ADDRESS", "")
    assert "HOME_ASSISTANT_URL" in run(IMPL_REGISTRY["smart_home"](action="list"))["message"]
    assert "EMAIL_ADDRESS" in run(IMPL_REGISTRY["check_email"]())["message"]


def test_send_email_is_two_step(monkeypatch):
    monkeypatch.setattr(config, "EMAIL_ADDRESS", "me@gmail.com")
    monkeypatch.setattr(config, "EMAIL_APP_PASSWORD", "x")
    sent = []
    import tools.email_tools as email_tools

    monkeypatch.setattr(email_tools, "_send", lambda *a: sent.append(a))
    assert run(email_tools._send_email(to="bad", subject="s", body="b"))["status"] == "error"
    first = run(email_tools._send_email(to="ivan@example.com", subject="Привет", body="Текст"))
    assert first["status"] == "needs_confirmation" and not sent
    assert run(email_tools._send_email(to="ivan@example.com", subject="Привет", body="Текст", confirm=True))["status"] == "ok"
    assert sent == [("ivan@example.com", "Привет", "Текст")]
    assert email_tools._hosts() == ("imap.gmail.com", "smtp.gmail.com")


def test_briefing_trigger(monkeypatch):
    said = []

    async def fake_brief(**kw):
        return {"status": "ok", "message": "Сводка готова."}

    async def fake_say(text):
        said.append(text)

    monkeypatch.setitem(IMPL_REGISTRY, "morning_briefing", fake_brief)
    monkeypatch.setattr(triggers.runtime, "say", fake_say)
    res = run(triggers._create_trigger(name="утро", when="daily", when_value="08:30", action="briefing",
                                       action_value="", confirm=True))
    assert res["status"] == "ok" and "рассказать сводку" in res["message"]
    trigger = run(triggers._store.load())[0]
    run(triggers.fire(trigger, "08:30"))
    assert said == ["Сводка готова."]


def test_wake_word_detector_runs_on_bundled_models():
    import numpy as np

    from wake_word import CHUNK, WakeWordDetector

    det = WakeWordDetector()
    rng = np.random.default_rng(0)
    scores = [det.score((rng.standard_normal(CHUNK) * 300).astype(np.int16)) for _ in range(30)]
    assert scores[0] == 0.0          # warm-up: not enough context yet
    assert max(scores) < 0.2         # noise is not "hey jarvis"


@pytest.mark.parametrize("name", ["get_news", "morning_briefing", "exchange_rate", "play_music", "smart_home",
                                  "check_email", "send_email", "list_reminders", "cancel_reminder", "write_clipboard"])
def test_new_tools_registered(name):
    assert name in IMPL_REGISTRY
