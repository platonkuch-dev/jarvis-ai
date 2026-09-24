"""One click / typed text / read inside a window -- cheap first, eyes second.

`quick_ui` tries Windows UI Automation first (free, instant, exact for
standard Windows controls) and, if that doesn't work, hands the very same
action to use_computer (tools/computer_use.py) on its own -- no second
round trip through the LLM, no question to the user.

It goes straight to use_computer, without trying UI Automation at all, when
UI Automation is known to be blind or the step is risky:
  * apps whose UI isn't exposed to UI Automation (Adobe, Electron apps like
    VS Code/Discord, browsers' page content, games) -- _SCREEN_ONLY_APPS;
  * apps that failed too often here recently (auto-learned, _Stats);
  * targets that sound irreversible (delete, send, pay, publish ...), since
    use_computer looks at the screen before and after acting and has its
    own rules for irreversible steps.

Only a real UI Automation hit counts as success (router strict mode): no
guessing coordinates by vision and no blind typing into whatever has focus,
which used to "succeed" while putting text in the wrong place.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Literal

from livekit.agents import RunContext, function_tool

import config
from tools._logging import log_call
from tools._store import read_json, write_json
from tools.registry import IMPL_REGISTRY, register_impl, register_tool

STATS_FILE = config.DATA_DIR / "quick_ui_stats.json"

# Substrings of an app's spoken/process name whose UI UI Automation can't see.
_SCREEN_ONLY_APPS = (
    "after effects", "afterfx", "photoshop", "premiere", "illustrator", "adobe",
    "vs code", "vscode", "code", "visual studio code", "cursor",
    "discord", "slack", "spotify", "telegram", "whatsapp", "notion", "obsidian", "figma",
    "chrome", "edge", "firefox", "opera", "яндекс", "yandex", "browser", "браузер",
    "steam", "epic", "game", "игр",
)
_RISKY = re.compile(
    r"удал|отправ|оплат|плат[её]ж|купи|заказ|опублик|форматир|стереть|сотри|сброс|выйти из аккаунта|"
    r"\b(delete|remove|send|pay|buy|order|publish|post|format|erase|reset|log ?out|sign ?out)\b",
    re.IGNORECASE,
)
# Auto-demotion: this many failures within the last _WINDOW attempts for an
# app sends it straight to use_computer from then on.
_WINDOW, _MAX_FAILS = 10, 4

QuickAction = Literal["click", "double_click", "right_click", "type", "read", "select"]


def _app_key(app: str) -> str:
    return app.strip().lower().removesuffix(".exe")


class _Stats:
    """Rolling success record per app, persisted so it learns across restarts."""

    @staticmethod
    def load() -> dict[str, list[int]]:
        data = read_json(STATS_FILE, {})
        return data if isinstance(data, dict) else {}

    @classmethod
    def record(cls, app: str, ok: bool) -> None:
        data = cls.load()
        history = (data.get(_app_key(app)) or [])[-(_WINDOW - 1):] + [1 if ok else 0]
        data[_app_key(app)] = history
        try:
            write_json(STATS_FILE, data)
        except OSError:
            pass

    @classmethod
    def demoted(cls, app: str) -> bool:
        history = cls.load().get(_app_key(app)) or []
        return history.count(0) >= _MAX_FAILS


def route(app: str, action: str, target: str, text: str = "") -> str | None:
    """None = try UI Automation first; otherwise the reason to go straight
    to the screen agent."""
    key = _app_key(app)
    if not key:
        return "не указано приложение"
    if any(re.search(rf"(^|\W){re.escape(name)}($|\W)", key) for name in _SCREEN_ONLY_APPS):
        return "интерфейс этого приложения не виден UI Automation"
    if action != "read" and _RISKY.search(f"{target} {text if action == 'select' else ''}"):
        return "действие может быть необратимым"
    if _Stats.demoted(app):
        return "UI Automation в этом приложении часто ошибался"
    return None


def _screen_task(app: str, action: str, target: str, text: str) -> str:
    where = f"В окне приложения «{app}»"
    return {
        "click": f"{where} нажми на элемент «{target}».",
        "double_click": f"{where} дважды щёлкни по элементу «{target}».",
        "right_click": f"{where} щёлкни правой кнопкой по элементу «{target}».",
        "type": f"{where} введи в поле «{target}» текст: {text}",
        "read": f"{where} прочитай, что написано в элементе «{target}», и ответь этим текстом.",
        "select": f"{where} в списке «{target}» выбери «{text}».",
    }[action]


async def _try_uia(app: str, action: str, target: str, text: str) -> tuple[bool, str]:
    from windows_control import router

    def _run():
        if action in ("click", "double_click", "right_click"):
            return router.click(target, app=app, double=action == "double_click",
                                button="right" if action == "right_click" else "left", strict=True)
        if action == "type":
            return router.type_into(text, query=target, app=app, strict=True)
        if action == "read":
            return router.read_element(target, app=app)
        if action == "select":
            return router.select_element(target, item=text, app=app)
        raise ValueError(action)

    result = await asyncio.to_thread(_run)
    return bool(result.success), result.message


@register_impl("quick_ui")
@log_call("quick_ui")
async def _quick_ui(*, app: str, action: str, target: str, text: str = "") -> dict:
    if config.SYSTEM != "Windows":
        return {"status": "error", "message": "Управление окнами поддерживается только на Windows."}
    if action not in ("click", "double_click", "right_click", "type", "read", "select"):
        return {"status": "error", "message": f"Неизвестное действие «{action}»."}
    if action in ("type", "select") and not text:
        return {"status": "error", "message": "Не указан текст."}

    reason = route(app, action, target, text)
    if reason is None:
        started = time.monotonic()
        try:
            ok, message = await _try_uia(app, action, target, text)
        except Exception as exc:
            ok, message = False, str(exc)
        _Stats.record(app, ok)
        if ok:
            return {"status": "ok", "message": message if action == "read" else "Готово.",
                    "method": "ui_automation", "seconds": round(time.monotonic() - started, 2)}
        reason = f"UI Automation не справился ({message})"

    use_computer = IMPL_REGISTRY["use_computer"]
    result = await use_computer(task=_screen_task(app, action, target, text))
    return {**result, "method": "use_computer", "why_screen": reason}


@register_tool
@function_tool
async def quick_ui(context: RunContext, app: str, action: QuickAction, target: str, text: str = "") -> str:
    """ONE simple action inside a window: press a button/menu item, type into a
    field, read a field, pick an item in a list. Tries the free, instant
    Windows UI Automation first and automatically falls back to use_computer
    (the screen-reading agent) on its own if that doesn't work -- so call this,
    not use_computer, for a single click/type/read. For multi-step work inside
    an app, use use_computer (or start_task) directly.

    Args:
        app: The application whose window it is, e.g. "Блокнот", "Параметры", "Word".
        action: click | double_click | right_click | type | read | select.
        target: The element's visible name/label, e.g. "Сохранить", "Имя файла".
        text: For type: the text to enter. For select: the item to pick.
    """
    result = await _quick_ui(app=app, action=action, target=target, text=text)
    return result["message"]
