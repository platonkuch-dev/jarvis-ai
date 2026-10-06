"""Promotion plan for the owner's project: what to post where and when.

data/content_plan.json holds a weekly schedule ("weekly": weekday + time),
dated one-off steps ("once") and undated to-dos ("todos"). `plan_loop()`
runs in the voice worker next to the reminder loop and, every few minutes,
copies the next 7 days of it into the ordinary planner files: each step
becomes a calendar event (so it shows in the day plan on the HUD/wallpaper
and in "что у меня завтра"), plus a reminder `remind_before` minutes ahead
(spoken, or sent to Telegram when nobody is at the PC). To-dos go to the
to-do list. data/content_plan_state.json remembers what was already copied,
so nothing is added twice and a step the owner deleted stays deleted.

Edit the plan by editing data/content_plan.json; the first run seeds it with
DEFAULT_PLAN. No LLM involved.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import date, datetime, timedelta

import config
from tools._store import JsonStore, read_json, write_json
from tools.scheduling import _events_store, _reminders_store, _todos_store

logger = logging.getLogger("jarvis-voice-agent.content_plan")

PLAN_FILE = config.DATA_DIR / "content_plan.json"
STATE_FILE = config.DATA_DIR / "content_plan_state.json"
_state_store = JsonStore(STATE_FILE, default={"done": []})

HORIZON_DAYS = 7
_POLL_S = 600.0
_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

SITE = "https://jarvis-ai-assistant-372.netlify.app"
ADMIN = "https://jarvis-ai-site-98w.pages.dev/admin.html"

DEFAULT_PLAN: dict = {
    "weekly": [
        {"id": "video", "days": ["mon", "wed", "fri"], "time": "18:30", "duration": 60, "remind_before": 30,
         "title": "🎬 Выложить ролик: TikTok + Shorts + Reels",
         "details": "Один ролик на все три площадки. Лучше всего запись экрана, где Джарвис реально что-то делает. "
                    "Первые 2 секунды — цепляющая фраза. После публикации 1–2 часа отвечай на комментарии, "
                    "на вопросы — видео-ответами."},
        {"id": "article", "days": ["tue"], "time": "15:00", "duration": 60, "remind_before": 30,
         "title": "✍️ Статья или пост о проекте (dev.to / DOU / Habr)",
         "details": f"Ссылка на сайт с меткой: {SITE}/?utm_source=devto (или dou, habr). Первые часы отвечай на комментарии."},
        {"id": "telegram", "days": ["wed"], "time": "12:00", "duration": 30, "remind_before": 0,
         "title": "📣 Написать 3 Telegram-каналам про ИИ/Python",
         "details": "Предложи пост о Джарвисе или взаимный пиар. В небольших каналах реклама стоит $5–20."},
        {"id": "fiverr", "days": _WEEKDAYS, "time": "12:30", "duration": 20, "remind_before": 0,
         "title": "💼 Fiverr: 5–10 откликов на запросы покупателей",
         "details": "Раздел Briefs / Buyer Requests. Первые заказы можно брать дешевле ради отзывов."},
        {"id": "review", "days": ["sun"], "time": "19:00", "duration": 30, "remind_before": 0,
         "title": "📊 Разбор недели в админке сайта",
         "details": f"Открой {ADMIN}: какой источник дал больше переходов, скачиваний и заявок — его и усиливай."},
    ],
    "once": [
        {"id": "devto-publish", "date": "2026-10-07", "time": "15:00", "duration": 30, "remind_before": 30,
         "title": "🚀 Опубликовать статью на dev.to",
         "details": "Черновик в Dashboard на dev.to. Внизу редактора отметь «AI Disclosure», проверь Preview и нажми Publish. "
                    "Первые часы отвечай на комментарии."},
        {"id": "show-hn", "date": "2026-10-08", "time": "15:00", "duration": 30, "remind_before": 60,
         "title": "🟠 Пост «Show HN» на Hacker News",
         "details": "Попроси Claude подготовить текст «Show HN: voice assistant that controls Windows» со ссылкой на GitHub."},
        {"id": "product-hunt", "date": "2026-10-10", "time": "10:00", "duration": 60, "remind_before": 60,
         "title": "🐱 Запуск на Product Hunt",
         "details": "Нужны картинки, видео и описание — попроси Claude подготовить страницу заранее."},
        {"id": "reddit-post", "date": "2026-10-12", "time": "15:00", "duration": 30, "remind_before": 30,
         "title": "🔁 Reddit: новый пост в r/SideProject",
         "details": "Текст в portfolio-site/ads/reddit_post.md, без ссылок, редактор в режиме Markdown. Ссылку на GitHub — "
                    "в комментарии. Если снова удалит фильтр — напиши модераторам (Message the mods)."},
    ] + [
        {"id": f"reddit-karma-{d}", "date": d, "time": "20:00", "duration": 20, "remind_before": 0,
         "title": "💬 Reddit: 3–5 полезных комментариев",
         "details": "Набираем карму перед постом: r/learnpython, r/Python, r/ClaudeAI, r/SideProject. Без ссылок."}
        for d in ("2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11")
    ],
    "todos": [
        {"id": "tiktok-link", "text": f"TikTok: ссылка в профиле → {SITE}/?utm_source=tiktok"},
        {"id": "tiktok-business", "text": "TikTok: перейти на бизнес-аккаунт, чтобы ссылка в профиле была кликабельной"},
        {"id": "reddit-email", "text": "Reddit: подтвердить почту и проверить теневой бан (r/ShadowBan)"},
        {"id": "reddit-link", "text": f"Reddit: ссылка в профиле → {SITE}/?utm_source=reddit (после набора кармы)"},
        {"id": "fiverr-gig", "text": "Fiverr: добавить в гиг видео и 3 пакета с ценами"},
        {"id": "admin-password", "text": "Пароль админки сайта → в менеджер паролей, потом удалить ADMIN_PASSWORD.txt"},
        {"id": "mirror-decide", "text": "Решить: удалить зеркало platonkuch-dev.github.io (в адресе имя)"},
        {"id": "reddit-old-post", "text": "Reddit: удалить старый пост, который убрал фильтр"},
    ],
}


_EMOJI = re.compile("[🀀-🫿☀-➿️‍]")


def _speakable(text: str) -> str:
    """Reminders are read out by TTS, which would spell out emoji names."""
    return re.sub(r"\s{2,}", " ", _EMOJI.sub("", text)).strip()


def load_plan() -> dict:
    if not PLAN_FILE.exists():
        write_json(PLAN_FILE, DEFAULT_PLAN)
    plan = read_json(PLAN_FILE, DEFAULT_PLAN)
    return plan if isinstance(plan, dict) else DEFAULT_PLAN


def _at(day: date, hhmm: str) -> datetime | None:
    try:
        h, m = (int(x) for x in str(hhmm).split(":", 1))
        return datetime(day.year, day.month, day.day, h, m)
    except (ValueError, TypeError):
        return None


def due_steps(plan: dict, today: date, horizon: int = HORIZON_DAYS) -> list[tuple[str, datetime, dict]]:
    """(state key, start, step) for every plan step in [today, today + horizon)."""
    out = []
    for offset in range(horizon):
        day = today + timedelta(days=offset)
        for step in plan.get("weekly") or []:
            if _WEEKDAYS[day.weekday()] in (step.get("days") or []):
                start = _at(day, step.get("time", ""))
                if start:
                    out.append((f"w:{step.get('id')}:{day.isoformat()}", start, step))
        for step in plan.get("once") or []:
            if step.get("date") == day.isoformat():
                start = _at(day, step.get("time", ""))
                if start:
                    out.append((f"o:{step.get('id')}", start, step))
    return out


async def materialize(now: datetime | None = None) -> int:
    """Copy not-yet-copied steps of the next HORIZON_DAYS into events/reminders/todos. Returns how many were added."""
    now = now or datetime.now()
    plan = load_plan()
    state = await _state_store.load()
    done = set(state.get("done") or [])
    events, reminders, todos, new_keys = [], [], [], []

    for key, start, step in due_steps(plan, now.date()):
        if key in done or start + timedelta(minutes=int(step.get("duration") or 30)) < now:
            continue                                 # already copied, or already over
        title = step.get("title", "")
        events.append({"id": uuid.uuid4().hex[:8], "title": title, "start": start.isoformat(),
                       "duration_minutes": int(step.get("duration") or 30), "source": "content_plan"})
        before = int(step.get("remind_before") or 0)
        when = start - timedelta(minutes=before)
        if when > now:
            lead = f"Через {before} минут: " if before else ""
            text = f"{lead}{title}. {step.get('details', '')}".strip()
            reminders.append({"id": uuid.uuid4().hex[:8], "text": _speakable(text), "at": when.isoformat(), "kind": "reminder"})
        new_keys.append(key)

    for item in plan.get("todos") or []:
        key = f"t:{item.get('id')}"
        if key not in done and item.get("text"):
            todos.append({"id": uuid.uuid4().hex[:8], "text": item["text"], "done": False,
                          "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
            new_keys.append(key)

    if not new_keys:
        return 0
    if events:
        await _events_store.mutate(lambda data: (data + events, None))
    if reminders:
        await _reminders_store.mutate(lambda data: (data + reminders, None))
    if todos:
        await _todos_store.mutate(lambda data: (data + todos, None))
    # forget keys of dated steps that are long gone, so the state file doesn't grow forever
    cutoff = (now.date() - timedelta(days=30)).isoformat()
    keep = [k for k in done if not (k.startswith("w:") and k.rsplit(":", 1)[-1] < cutoff)]
    await _state_store.mutate(lambda _s: ({"done": sorted(set(keep) | set(new_keys))}, None))
    logger.info("content plan: added %d event(s), %d reminder(s), %d todo(s)", len(events), len(reminders), len(todos))
    return len(new_keys)


async def plan_loop() -> None:
    while True:
        try:
            await materialize()
        except Exception:
            logger.exception("content plan tick failed")
        await asyncio.sleep(_POLL_S)


def summary(days: int = HORIZON_DAYS) -> str:
    """Plain-text overview of the coming days (for logs and tests)."""
    lines = []
    for _key, start, step in sorted(due_steps(load_plan(), date.today(), days), key=lambda x: x[1]):
        lines.append(f"{start:%a %d.%m %H:%M}  {step.get('title', '')}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(json.dumps({"plan_file": str(PLAN_FILE)}, ensure_ascii=False))
    print(summary())
