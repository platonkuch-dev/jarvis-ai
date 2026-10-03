"""What the HUD panel shows besides Jarvis himself: today's events, open
todos, running timers/reminders, weather, the owner's name.

Plain file reads + one cached Open-Meteo call, no livekit and no LLM, so the
panel server (hud_panel.py) can run in the HUD process as well as in the
voice worker.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import config

_WEATHER_TTL_S = 900.0
_weather_lock = threading.Lock()
_weather_cache: dict = {"at": 0.0, "city": "", "data": None}

_CODES = {0: "ясно", 1: "преимущественно ясно", 2: "переменная облачность", 3: "пасмурно", 45: "туман",
          48: "изморозь", 51: "лёгкая морось", 53: "морось", 55: "сильная морось", 61: "небольшой дождь",
          63: "дождь", 65: "сильный дождь", 71: "небольшой снег", 73: "снег", 75: "сильный снегопад",
          80: "ливень", 81: "сильный ливень", 82: "очень сильный ливень", 95: "гроза", 96: "гроза с градом"}


def _load(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _memory_value(*keys: str) -> str:
    memory = _load(config.MEMORY_FILE, {})
    if not isinstance(memory, dict):
        return ""
    for category in memory.values():
        if not isinstance(category, dict):
            continue
        for key, entry in category.items():
            if key.lower() in keys and isinstance(entry, dict):
                return str(entry.get("value", ""))
    return ""


def _weather(city: str) -> tuple[dict | None, str]:
    if not city:
        return None, ""
    with _weather_lock:
        if _weather_cache["city"] == city and time.time() - _weather_cache["at"] < _WEATHER_TTL_S:
            return _weather_cache["data"], _weather_cache.get("name", city)
    data, name = None, city
    try:
        import httpx

        with httpx.Client(timeout=8) as client:
            geo = client.get(config.OPEN_METEO_GEOCODE_URL, params={"name": city, "count": 1, "language": "ru"}).json()
            place = (geo.get("results") or [None])[0]
            if place:
                fc = client.get(config.OPEN_METEO_FORECAST_URL, params={
                    "latitude": place["latitude"], "longitude": place["longitude"], "current_weather": "true",
                    "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                    "forecast_days": 1, "timezone": "auto"}).json()
                cur, daily = fc.get("current_weather") or {}, fc.get("daily") or {}
                data = {"temp": cur.get("temperature"), "condition": _CODES.get(int(cur.get("weathercode", -1)), ""),
                        "min": (daily.get("temperature_2m_min") or [None])[0],
                        "max": (daily.get("temperature_2m_max") or [None])[0],
                        "rain": (daily.get("precipitation_probability_max") or [None])[0]}
                name = place.get("name", city)
    except Exception:
        pass
    with _weather_lock:
        _weather_cache.update(at=time.time(), city=city, data=data, name=name)
    return data, name


def collect(day_offset: int = 0) -> dict:
    """`day_offset` shifts which day's events are returned (0 = today, 1 =
    tomorrow, -1 = yesterday...) -- todos/reminders/weather stay as they are,
    only the events ring/list is date-scoped."""
    today = datetime.now().date() + timedelta(days=day_offset)
    events = []
    for e in _load(config.EVENTS_FILE, []):
        try:
            start = datetime.fromisoformat(e["start"])
        except (KeyError, ValueError, TypeError):
            continue
        if start.date() == today:
            events.append({"title": e.get("title", ""), "start": start.hour * 60 + start.minute,
                           "duration": int(e.get("duration_minutes") or 30)})
    todos = [t.get("text", "") for t in _load(config.TODOS_FILE, []) if isinstance(t, dict) and not t.get("done")]
    reminders = []
    for r in _load(config.REMINDERS_FILE, []):
        try:
            reminders.append({"text": r.get("text", ""), "kind": r.get("kind", "reminder"),
                              "at": datetime.fromisoformat(r["at"]).timestamp()})
        except (KeyError, ValueError, TypeError):
            continue
    city = config.HOME_CITY or _memory_value("city", "город", "home_city")
    weather, city_name = _weather(city)
    return {"events": events, "todos": todos, "reminders": reminders, "weather": weather,
            "city": city_name, "name": _memory_value("name", "имя")}
