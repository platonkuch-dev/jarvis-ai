"""
Fast Path — matches simple, unambiguous commands directly against existing
Rust/Python tools (actions/window_control.py, actions/open_app.py,
actions/computer_settings.py) and executes them immediately. Claude/Gemini
is never called for anything that matches here — that's the entire point:
"открой Telegram" shouldn't cost a network round-trip to a reasoning model.

Deliberately narrow and literal (regex, not fuzzy NLU) — per the project's
own stated priority ladder (exact command > regex > classifier > LLM), this
layer should only ever handle things it's CERTAIN about. Anything that
doesn't match falls through to the Smart Path (Claude) unchanged; a wrong
guess executed at low latency is worse than a slightly slower correct one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

FastHandler = Callable[[re.Match], str]


@dataclass
class FastRule:
    pattern: re.Pattern
    handler: FastHandler
    name: str


@dataclass
class RouteResult:
    matched: bool
    rule_name: str = ""
    success: bool = False
    message: str = ""


def _open_app(m: re.Match) -> str:
    from actions.window_control import launch_and_verify
    return launch_and_verify(m.group("app").strip())


def _open_folder(m: re.Match) -> str:
    from actions.window_control import launch_and_verify
    return launch_and_verify(m.group("folder").strip().lower())


def _close_named_app(m: re.Match) -> str:
    from actions.window_control import window_manager
    return window_manager(parameters={"action": "close", "app": m.group("app").strip()})


def _close_active(_m: re.Match) -> str:
    from actions.window_control import window_manager
    return window_manager(parameters={"action": "close", "app": ""})


def _minimize_active(_m: re.Match) -> str:
    from actions.window_control import window_manager
    return window_manager(parameters={"action": "minimize", "app": ""})


def _maximize_active(_m: re.Match) -> str:
    from actions.window_control import window_manager
    return window_manager(parameters={"action": "maximize", "app": ""})


def _switch_to(m: re.Match) -> str:
    from actions.window_control import window_manager
    return window_manager(parameters={"action": "focus", "app": m.group("app").strip()})


def _set_volume(m: re.Match) -> str:
    from actions.computer_settings import computer_settings
    value = max(0, min(100, int(m.group("value"))))
    return computer_settings(parameters={"action": "volume_set", "value": value})


def _mute(_m: re.Match) -> str:
    from actions.computer_settings import computer_settings
    return computer_settings(parameters={"action": "mute"})


def _volume_up(_m: re.Match) -> str:
    from actions.computer_settings import computer_settings
    return computer_settings(parameters={"action": "volume_up"})


def _volume_down(_m: re.Match) -> str:
    from actions.computer_settings import computer_settings
    return computer_settings(parameters={"action": "volume_down"})


def _screenshot(_m: re.Match) -> str:
    from actions.computer_settings import computer_settings
    return computer_settings(parameters={"action": "screenshot"})


# Zero-argument handlers, addressable by intent name instead of by regex
# match — reused by voice/intent_classifier.py's MiniLM fuzzy-match tier so
# a paraphrase like "подними звук" dispatches through the exact same code
# path as the regex-matched "громче", not a duplicate implementation.
INTENT_DISPATCH: dict[str, FastHandler] = {
    "mute": _mute,
    "volume_up": _volume_up,
    "volume_down": _volume_down,
    "screenshot": _screenshot,
    "minimize": _minimize_active,
    "maximize": _maximize_active,
    "close_active": _close_active,
}


# Order matters: more specific patterns must come before their generic
# fallbacks (e.g. "закрой программу" — no name — before "закрой <app>").
_RULES: list[FastRule] = [
    FastRule(
        re.compile(r"^(?:открой|запусти|включи)\s+папку\s+(?P<folder>downloads|загрузки|desktop|рабочий стол|documents|документы)$", re.I),
        _open_folder, "open_folder",
    ),
    FastRule(
        re.compile(r"^закрой\s+(?:программу|приложение|окно)$", re.I),
        _close_active, "close_active",
    ),
    # "выключи звук"/"без звука" mean mute, not close — mute's own rule
    # further down would never be reached if a generic "закрой|выключи
    # <anything>" pattern came first and swallowed "звук" as an app name.
    # Keeping close_app to "закрой" only (a clear, unambiguous verb for
    # closing an app) avoids that collision entirely.
    FastRule(
        re.compile(r"^закрой\s+(?:программу\s+|приложение\s+)?(?P<app>[\w\sа-яё]+?)$", re.I),
        _close_named_app, "close_app",
    ),
    FastRule(
        re.compile(r"^(?:сверни|минимизируй)\s+(?:окно)?$", re.I),
        _minimize_active, "minimize",
    ),
    FastRule(
        re.compile(r"^(?:разверни|максимизируй)\s+(?:окно)?$", re.I),
        _maximize_active, "maximize",
    ),
    FastRule(
        re.compile(r"^(?:переключись|перейди)\s+на\s+(?P<app>[\w\sа-яё]+?)$", re.I),
        _switch_to, "switch_to",
    ),
    FastRule(
        re.compile(r"^(?:громкость|поставь громкость(?:\s+на)?|установи громкость(?:\s+на)?)\s+(?P<value>\d{1,3})(?:\s*%)?$", re.I),
        _set_volume, "set_volume",
    ),
    FastRule(
        re.compile(r"^(?:выключи звук|заглуши|без звука|мьют)$", re.I),
        _mute, "mute",
    ),
    FastRule(
        re.compile(r"^(?:громче|прибавь громкость|увеличь громкость)$", re.I),
        _volume_up, "volume_up",
    ),
    FastRule(
        re.compile(r"^(?:тише|убавь громкость|уменьши громкость)$", re.I),
        _volume_down, "volume_down",
    ),
    FastRule(
        re.compile(r"^(?:сделай скриншот|скриншот|сфотографируй экран)$", re.I),
        _screenshot, "screenshot",
    ),
    FastRule(
        re.compile(r"^(?:открой|запусти|включи)\s+(?:программу\s+|приложение\s+)?(?P<app>[\w\sа-яё]+?)$", re.I),
        _open_app, "open_app",
    ),
]


# Wake-word remnants and politeness filler a real utterance may still carry
# even after the wake-word detector itself consumes the trigger phrase —
# STT can bleed a trailing syllable of "Джарвис" into the transcript, and
# people naturally say "пожалуйста"/"будь добр" around a command. Stripped
# from the FRONT only, and only greedily up to where a real command verb
# would plausibly start — never touches the app/value captured by a rule.
_FILLER_PREFIX = re.compile(
    r"^(?:джарвис|джарв(?:ис)?|джервис|джайвис|эй\s+джарвис)?[,.\s]*"
    r"(?:пожалуйста|будь добр[а]?|скажи|слушай)?[,.\s]*",
    re.I,
)


def route(text: str) -> RouteResult:
    """Try every fast rule in order; the first match wins. Never raises —
    a handler exception is reported as a failed-but-matched route (so the
    caller knows NOT to also forward this to Claude, since the user's
    intent WAS understood, just execution failed)."""
    normalized = text.strip().rstrip(".!?")
    if not normalized:
        return RouteResult(matched=False)
    normalized = _FILLER_PREFIX.sub("", normalized, count=1).strip()
    if not normalized:
        return RouteResult(matched=False)

    for rule in _RULES:
        m = rule.pattern.match(normalized)
        if not m:
            continue
        try:
            message = rule.handler(m)
            return RouteResult(matched=True, rule_name=rule.name, success=True, message=message)
        except Exception as e:
            return RouteResult(matched=True, rule_name=rule.name, success=False, message=f"Fast path '{rule.name}' failed: {e}")

    return RouteResult(matched=False)
