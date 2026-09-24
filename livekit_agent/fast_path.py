"""Zero-cost local handling of the most common short voice commands.

Most of what gets said to a desktop assistant is "громкость 30", "пауза",
"таймер на пять минут", "открой телеграм", "который час". Sending each of
those through the LLM costs a full round trip with ~11k tokens of cached
prompt and adds a second of latency, only for the model to call one obvious
tool. `parse()` recognises exactly those phrasings -- whole-utterance
matches only, so anything longer or ambiguous still goes to the LLM -- and
`execute()` runs the same IMPL_REGISTRY function the LLM would have called.

Deliberately conservative: a false positive (doing the wrong thing without
the LLM) is worse than a miss (the LLM handles it, as before). Pure regex,
no model, no network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Spoken app names (as Deepgram writes them in Russian) -> the name the
# launcher / process matcher understands.
APP_NAMES: dict[str, str] = {
    "хром": "chrome", "гугл хром": "chrome", "google chrome": "chrome", "браузер": "chrome",
    "телеграм": "telegram", "телеграмм": "telegram", "телега": "telegram",
    "дискорд": "discord", "спотифай": "spotify", "слак": "slack", "зум": "zoom",
    "стим": "steam", "обсидиан": "obsidian", "тимс": "teams",
    "блокнот": "notepad", "калькулятор": "calculator", "проводник": "explorer",
    "терминал": "terminal", "командную строку": "cmd", "командная строка": "cmd",
    "ворд": "word", "эксель": "excel", "аутлук": "outlook",
    "фотошоп": "photoshop", "премьер": "premiere pro", "премьер про": "premiere pro",
    "афтер эффектс": "after effects", "афтер": "after effects", "иллюстратор": "illustrator",
    "вс код": "vs code", "вскод": "vs code", "визуал студио код": "vs code", "vs code": "vs code",
    "эдж": "edge", "файрфокс": "firefox", "фаерфокс": "firefox",
}
# Process names that must never be killed by a fuzzy "закрой ...".
_PROTECTED = {"explorer", "проводник", "windows", "system", "python", "jarvis", "джарвис"}

_NUMBER_WORDS = {
    "ноль": 0, "один": 1, "одну": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
    "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
    "одиннадцать": 11, "двенадцать": 12, "пятнадцать": 15, "двадцать": 20,
    "двадцать пять": 25, "тридцать": 30, "сорок": 40, "сорок пять": 45, "пятьдесят": 50,
    "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90, "сто": 100,
    "полтора": 1.5, "полторы": 1.5,
}


@dataclass
class Intent:
    tool: str | None           # IMPL_REGISTRY name, or None for a local answer
    args: dict[str, Any] = field(default_factory=dict)
    answer: str | None = None  # for tool=None: what to say


def normalize(text: str) -> str:
    t = text.lower().replace("ё", "е")
    t = re.sub(r"[^\w\s%+-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"^(джарвис|jarvis)\s+", "", t)
    t = re.sub(r"\s*(пожалуйста|плиз)\s*", " ", t).strip()
    return t


def _number(token: str) -> float | None:
    token = token.strip()
    if re.fullmatch(r"\d+([.,]\d+)?", token):
        return float(token.replace(",", "."))
    return _NUMBER_WORDS.get(token)


def _app(name: str) -> str | None:
    """Known app for a spoken name, or the name itself if it's plain ASCII
    (Deepgram already wrote it in Latin letters, e.g. "chrome")."""
    name = name.strip()
    if name in APP_NAMES:
        return APP_NAMES[name]
    if re.fullmatch(r"[a-z][a-z0-9 .+-]{2,30}", name):
        return name
    return None


_UNIT_S = {"сек": 1, "мин": 60, "час": 3600}


def parse(text: str, *, computer_use_running: bool = False) -> Intent | None:
    t = normalize(text)
    if not t or len(t) > 60:
        return None

    if computer_use_running and t in ("стоп", "хватит", "остановись", "стой", "отмена", "прекрати"):
        return Intent("stop_computer_use")

    m = re.fullmatch(r"(?:сделай |поставь |установи |выставь )?(громкость|звук|яркость)(?: на)? (\S+)(?: процент\w*| %|%)?", t)
    if m:
        value = _number(m.group(2).rstrip("%"))
        if value is not None and 0 <= value <= 100:
            action = "brightness" if m.group(1) == "яркость" else "volume"
            return Intent("system_control", {"action": action, "value": int(value)})

    if re.fullmatch(r"(?:сделай )?(?:по)?громче|прибавь(?: звук| громкость)?|(?:сделай )?звук громче", t):
        return Intent("volume_step", {"delta": 10})
    if re.fullmatch(r"(?:сделай )?(?:по)?тише|убавь(?: звук| громкость)?|(?:сделай )?звук тише", t):
        return Intent("volume_step", {"delta": -10})
    if t in ("выключи звук", "без звука", "отключи звук", "мьют", "замьють"):
        return Intent("system_control", {"action": "volume", "value": 0})

    if t in ("пауза", "на паузу", "поставь на паузу", "поставь паузу", "продолжи воспроизведение",
             "продолжи музыку", "сними с паузы", "плей"):
        return Intent("media_control", {"action": "pause"})
    if re.fullmatch(r"(?:включи |переключи на )?следующ(?:ий|ую|ая) (?:трек|песн[юя]|композици[юя])|переключи трек", t):
        return Intent("media_control", {"action": "next"})
    if re.fullmatch(r"(?:включи |верни )?предыдущ(?:ий|ую|ая) (?:трек|песн[юя]|композици[юя])", t):
        return Intent("media_control", {"action": "prev"})

    m = re.fullmatch(r"(?:поставь |заведи |запусти |засеки )?таймер на (.+?) (сек\w*|мин\w*|час\w*)", t)
    if m:
        amount = _number(m.group(1))
        if amount is not None and amount > 0:
            seconds = amount * _UNIT_S[m.group(2)[:3]]
            return Intent("set_timer", {"minutes": round(seconds / 60, 2)})
    if re.fullmatch(r"(?:поставь |заведи )?таймер на полчаса", t):
        return Intent("set_timer", {"minutes": 30})
    m = re.fullmatch(r"(?:поставь |заведи )?таймер на (минуту|час)", t)
    if m:
        return Intent("set_timer", {"minutes": 1 if m.group(1) == "минуту" else 60})

    if t in ("который час", "сколько времени", "сколько сейчас времени", "который сейчас час"):
        return Intent(None, answer=f"Сейчас {datetime.now().strftime('%H:%M')}.")
    if t in ("какое сегодня число", "какой сегодня день", "какое число", "какая сегодня дата"):
        return Intent(None, answer=_today_phrase())

    if re.fullmatch(r"заблокируй (?:компьютер|экран|пк|комп)", t):
        return Intent("system_control", {"action": "lock"})

    m = re.fullmatch(r"(?:открой|запусти) (?:приложение |программу )?(.{2,30})", t)
    if m:
        app = _app(m.group(1))
        if app:
            return Intent("open_application", {"name": app})
    m = re.fullmatch(r"закрой (?:приложение |программу )?(.{2,30})", t)
    if m:
        app = _app(m.group(1))
        if app and app not in _PROTECTED and m.group(1) not in _PROTECTED:
            return Intent("close_application", {"name": app})
    return None


_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
           "сентября", "октября", "ноября", "декабря"]


def _today_phrase(now: datetime | None = None) -> str:
    now = now or datetime.now()
    return f"Сегодня {_WEEKDAYS[now.weekday()]}, {now.day} {_MONTHS[now.month - 1]}."


def _volume_step(delta: int) -> str:
    from ctypes import POINTER, cast

    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

    devices = AudioUtilities.GetSpeakers()
    interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    volume = cast(interface, POINTER(IAudioEndpointVolume))
    current = round(volume.GetMasterVolumeLevelScalar() * 100)
    new = max(0, min(100, current + delta))
    volume.SetMasterVolumeLevelScalar(new / 100.0, None)
    return f"Громкость {new}%."


async def execute(intent: Intent) -> tuple[bool, str]:
    """-> (handled, text to say). handled=False means "let the LLM do it"."""
    import asyncio

    from tools.registry import IMPL_REGISTRY

    if intent.tool is None:
        return True, intent.answer or ""
    if intent.tool == "volume_step":
        try:
            return True, await asyncio.to_thread(_volume_step, intent.args["delta"])
        except Exception:
            return False, ""
    impl = IMPL_REGISTRY.get(intent.tool)
    if impl is None:
        return False, ""
    try:
        result = await impl(**intent.args)
    except Exception:
        return False, ""
    message = result.get("message", "Готово.") if isinstance(result, dict) else "Готово."
    return True, message
