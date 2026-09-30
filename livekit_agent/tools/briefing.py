"""News headlines (RSS/Atom, no API key) and a morning briefing.

`morning_briefing` is assembled from other tools' impls -- weather, today's
schedule, open todos, headlines, PC state -- with no LLM call at all, so a
daily trigger can speak it for free (triggers.py's "briefing" action).
"""

from __future__ import annotations

import asyncio
import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime

import httpx
from livekit.agents import RunContext, function_tool

import config
from tools._logging import log_call
from tools.registry import IMPL_REGISTRY, register_impl, register_tool

_ATOM = "{http://www.w3.org/2005/Atom}"


def parse_feeds(raw: str) -> list[tuple[str, str]]:
    """"name=url,url2" -> [(name, url), (host, url2)]."""
    feeds = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, url = part.partition("=") if "=" in part and not part.startswith("http") else ("", "", part)
        url = url.strip()
        name = name.strip() or re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
        feeds.append((name, url))
    return feeds


def parse_rss(xml_text: str, limit: int) -> list[dict]:
    """RSS 2.0 and Atom items -> [{"title", "link", "summary"}]."""
    root = ET.fromstring(xml_text)
    items = root.findall(".//item") or root.findall(f".//{_ATOM}entry")
    out = []
    for item in items[:limit]:
        title = item.findtext("title") or item.findtext(f"{_ATOM}title") or ""
        link = item.findtext("link") or ""
        if not link:
            link_el = item.find(f"{_ATOM}link")
            link = link_el.get("href", "") if link_el is not None else ""
        summary = item.findtext("description") or item.findtext(f"{_ATOM}summary") or ""
        summary = html.unescape(re.sub(r"<[^>]+>", " ", summary))
        summary = re.sub(r"\s+", " ", summary).strip()[:200]
        if title.strip():
            out.append({"title": html.unescape(title.strip()), "link": link.strip(), "summary": summary})
    return out


async def _fetch_feed(client: httpx.AsyncClient, name: str, url: str, limit: int) -> list[dict]:
    try:
        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 JarvisNews"})
        resp.raise_for_status()
        items = parse_rss(resp.text, limit)
    except Exception:
        return []
    for item in items:
        item["source"] = name
    return items


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?", "…")) else text + "."


@register_impl("get_news")
@log_call("get_news")
async def _get_news(*, topic: str = "", limit: int = 5) -> dict:
    limit = max(1, min(int(limit or 5), 15))
    feeds = parse_feeds(config.NEWS_FEEDS)
    if not feeds:
        return {"status": "error", "message": "Не задан ни один источник новостей (NEWS_FEEDS)."}

    # Short timeout: a blocked feed (they differ by country) must not stall the answer.
    async with httpx.AsyncClient(timeout=5, follow_redirects=True) as client:
        per_feed = 30 if topic else limit
        batches = await asyncio.gather(*(_fetch_feed(client, n, u, per_feed) for n, u in feeds))

    items = [i for batch in batches for i in batch]
    if topic:
        words = [w for w in re.findall(r"\w+", topic.lower()) if len(w) > 2]
        # Crude Russian stemming: match on the first 5 letters so "выборы"/"выборах" both hit.
        stems = [w[:5] for w in words]
        items = [i for i in items if any(s in (i["title"] + " " + i["summary"]).lower() for s in stems)]
    else:
        # Round-robin across sources so one chatty feed doesn't fill the list.
        items = [i for group in zip(*[b for b in batches if b]) for i in group] or items

    if not items:
        what = f" по теме «{topic}»" if topic else ""
        return {"status": "not_found", "message": f"Свежих новостей{what} не нашёл."}

    items = items[:limit]
    lines = [f"{n}. {i['title']} ({i['source']})" for n, i in enumerate(items, 1)]
    message = "\n".join(lines) + "\n\nПодробности — read_webpage по ссылке: " + " | ".join(
        i["link"] for i in items if i["link"])
    speech = "Главные новости. " + " ".join(_sentence(i["title"]) for i in items)
    return {"status": "ok", "message": message, "speech": speech, "items": items}


@register_tool
@function_tool
async def get_news(context: RunContext, topic: str = "", limit: int = 5) -> str:
    """Fresh news headlines from RSS feeds (free, no LLM). Read out only the
    headlines briefly; open one with read_webpage if the user wants details.

    Args:
        topic: Optional keyword filter ("технологии", "биткоин", "Украина"). Empty = top news.
        limit: How many headlines (1-15), default 5.
    """
    result = await _get_news(topic=topic, limit=limit)
    return result["message"]


# ---------------------------------------------------------------------------
# morning_briefing
# ---------------------------------------------------------------------------


async def _home_city() -> str:
    if config.HOME_CITY:
        return config.HOME_CITY
    try:
        from tools.memory import load_memory

        memory = await load_memory()
        for category in memory.values():
            for key, entry in category.items():
                if key.lower() in ("city", "город", "home_city") and isinstance(entry, dict):
                    return str(entry.get("value", ""))
    except Exception:
        pass
    return ""


def _greeting(now: datetime) -> str:
    if 5 <= now.hour < 12:
        return "Доброе утро"
    if 12 <= now.hour < 18:
        return "Добрый день"
    if 18 <= now.hour < 23:
        return "Добрый вечер"
    return "Доброй ночи"


async def _call(name: str, **kwargs) -> dict:
    impl = IMPL_REGISTRY.get(name)
    if impl is None:
        return {}
    try:
        return await impl(**kwargs)
    except Exception:
        return {}


@register_impl("morning_briefing")
@log_call("morning_briefing")
async def _morning_briefing(*, city: str = "", news: bool = True) -> dict:
    now = datetime.now()
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря"]
    weekdays = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    parts = [f"{_greeting(now)}! Сегодня {weekdays[now.weekday()]}, {now.day} {months[now.month - 1]}, "
             f"{now.strftime('%H:%M')}."]

    city = city or await _home_city()
    todos_task = asyncio.to_thread(_open_todos)
    coros = {
        "weather": _call("get_weather", location=city, days=1) if city else asyncio.sleep(0, {}),
        "schedule": _call("get_schedule", date_str=now.strftime("%Y-%m-%d")),
        "news": _call("get_news", limit=3) if news else asyncio.sleep(0, {}),
        "system": _call("get_system_status"),
    }
    results = dict(zip(coros, await asyncio.gather(*coros.values())))
    todos = await todos_task

    if results["weather"].get("status") == "ok":
        parts.append(results["weather"]["message"])
    if results["schedule"].get("events"):
        parts.append(results["schedule"]["message"])
    else:
        parts.append("Событий в календаре на сегодня нет.")
    if todos:
        shown = "; ".join(todos[:5])
        more = f" и ещё {len(todos) - 5}" if len(todos) > 5 else ""
        parts.append(f"Открытых дел: {len(todos)} — {shown}{more}.")
    if results["news"].get("items"):
        heads = "; ".join(i["title"] for i in results["news"]["items"])
        parts.append(f"Главное в новостях: {heads}.")
    sysinfo = results["system"]
    if sysinfo.get("disk_percent", 0) >= 90:
        parts.append(f"Внимание: диск заполнен на {sysinfo['disk_percent']:.0f}%.")
    if sysinfo.get("battery_percent") is not None and sysinfo["battery_percent"] < 20 and not sysinfo.get("battery_plugged"):
        parts.append(f"Батарея садится: {sysinfo['battery_percent']}%.")
    return {"status": "ok", "message": " ".join(parts)}


def _open_todos() -> list[str]:
    import json

    try:
        data = json.loads(config.TODOS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [t.get("text", "") for t in data if isinstance(t, dict) and not t.get("done")]


@register_tool
@function_tool
async def morning_briefing(context: RunContext, city: str = "", news: bool = True) -> str:
    """Daily briefing in one go: date, weather, today's schedule, open todos,
    top headlines and PC warnings ("что у меня сегодня", "утренняя сводка",
    "брифинг"). Free -- no LLM inside; speak the result as is, briefly.

    Args:
        city: City for the weather; empty = the user's home city from memory.
        news: Include top headlines.
    """
    result = await _morning_briefing(city=city, news=news)
    return result["message"]
