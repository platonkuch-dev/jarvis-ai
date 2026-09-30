"""Smart home through Home Assistant's REST API (lights, plugs, climate,
covers, media players, scenes, scripts -- whatever HA already controls).

One HA instance covers Hue, Tuya, Xiaomi, Shelly, Zigbee, Matter, Yandex
devices bridged into HA, etc., so Jarvis talks to HA only. Entities are
matched by their friendly name the way people say them ("свет на кухне").
"""

from __future__ import annotations

from typing import Literal

import httpx
from livekit.agents import RunContext, function_tool

import config
from tools._logging import log_call
from tools.registry import register_impl, register_tool

HomeAction = Literal["list", "state", "on", "off", "toggle", "brightness", "temperature", "scene"]

_CONTROLLABLE = ("light", "switch", "fan", "climate", "cover", "media_player", "scene", "script",
                 "input_boolean", "lock", "vacuum", "humidifier")


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {config.HOME_ASSISTANT_TOKEN}", "Content-Type": "application/json"}


def _name(entity: dict) -> str:
    return (entity.get("attributes") or {}).get("friendly_name") or entity["entity_id"]


def match_entity(states: list[dict], query: str, domains: tuple[str, ...] = _CONTROLLABLE) -> dict | None:
    """Best entity for a spoken name: exact id, exact name, then every word contained."""
    q = query.strip().lower()
    pool = [s for s in states if s["entity_id"].split(".")[0] in domains]
    for s in pool:
        if s["entity_id"].lower() == q or _name(s).lower() == q:
            return s
    words = [w[:5] for w in q.split() if len(w) > 1]
    scored = []
    for s in pool:
        hay = (_name(s) + " " + s["entity_id"]).lower()
        hits = sum(1 for w in words if w in hay)
        if hits:
            scored.append((hits, -len(hay), s))
    if not scored:
        return None
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return scored[0][2]


@register_impl("smart_home")
@log_call("smart_home")
async def _smart_home(*, action: str, entity: str = "", value: str = "") -> dict:
    if not (config.HOME_ASSISTANT_URL and config.HOME_ASSISTANT_TOKEN):
        return {"status": "error", "message": (
            "Умный дом не настроен: укажите HOME_ASSISTANT_URL и HOME_ASSISTANT_TOKEN в панели "
            "(токен — в профиле Home Assistant, «Долгосрочные токены доступа»).")}
    base = config.HOME_ASSISTANT_URL
    try:
        async with httpx.AsyncClient(timeout=10, headers=_headers()) as client:
            resp = await client.get(f"{base}/api/states")
            resp.raise_for_status()
            states: list[dict] = resp.json()

            if action == "list":
                pool = [s for s in states if s["entity_id"].split(".")[0] in _CONTROLLABLE
                        and s["entity_id"].split(".")[0] not in ("script",)]
                if entity:
                    pool = [s for s in pool if s["entity_id"].startswith(entity) or entity.lower() in _name(s).lower()]
                lines = [f"{_name(s)} — {s['state']}" for s in pool[:40]]
                return {"status": "ok", "message": "; ".join(lines) or "Устройств не найдено."}

            domains = ("scene", "script") if action == "scene" else _CONTROLLABLE
            target = match_entity(states, entity, domains)
            if target is None:
                return {"status": "not_found", "message": f"Не нашёл в умном доме «{entity}»."}
            eid = target["entity_id"]
            domain = eid.split(".")[0]
            name = _name(target)

            if action == "state":
                attrs = target.get("attributes") or {}
                extra = []
                for key, label in (("current_temperature", "температура"), ("temperature", "уставка"),
                                   ("brightness", "яркость"), ("unit_of_measurement", "")):
                    if key in attrs and key != "unit_of_measurement":
                        val = attrs[key]
                        if key == "brightness" and isinstance(val, (int, float)):
                            val = f"{round(val / 255 * 100)}%"
                        extra.append(f"{label} {val}")
                unit = attrs.get("unit_of_measurement", "")
                return {"status": "ok", "message": f"{name}: {target['state']}{unit and ' ' + unit}"
                                                   + (f" ({', '.join(extra)})" if extra else "") + "."}

            service, data = _service_for(action, domain, value)
            if service is None:
                return {"status": "error", "message": data.get("error", "Неподдерживаемое действие.")}
            data["entity_id"] = eid
            call = await client.post(f"{base}/api/services/{service}", json=data)
            call.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            return {"status": "error", "message": "Home Assistant отклонил токен (401)."}
        return {"status": "error", "message": f"Ошибка Home Assistant: {exc}"}
    except Exception as exc:
        return {"status": "error", "message": f"Не удалось связаться с Home Assistant: {exc}"}

    done = {"on": "включил", "off": "выключил", "toggle": "переключил", "brightness": f"поставил яркость {value}%",
            "temperature": f"поставил {value}°", "scene": "запустил"}[action]
    return {"status": "ok", "message": f"{name}: {done}."}


def _service_for(action: str, domain: str, value: str) -> tuple[str | None, dict]:
    if action == "scene":
        return ("scene/turn_on" if domain == "scene" else "script/turn_on"), {}
    if action in ("on", "off", "toggle"):
        if domain == "cover":
            svc = {"on": "open_cover", "off": "close_cover", "toggle": "toggle"}[action]
            return f"cover/{svc}", {}
        if domain == "lock":
            return (f"lock/{'unlock' if action == 'on' else 'lock'}", {}) if action != "toggle" else (None, {"error": "Замок можно только открыть или закрыть."})
        if domain in ("scene", "script"):
            return f"{domain}/turn_on", {}
        return f"homeassistant/turn_{action}" if action != "toggle" else "homeassistant/toggle", {}
    if action == "brightness":
        if domain != "light":
            return None, {"error": "Яркость есть только у света."}
        try:
            pct = max(0, min(100, int(float(value))))
        except ValueError:
            return None, {"error": "Яркость — число от 0 до 100."}
        return "light/turn_on", {"brightness_pct": pct}
    if action == "temperature":
        if domain != "climate":
            return None, {"error": "Температуру можно задать только климатическому устройству."}
        try:
            return "climate/set_temperature", {"temperature": float(value)}
        except ValueError:
            return None, {"error": "Температура — число."}
    return None, {"error": f"Неизвестное действие «{action}»."}


@register_tool
@function_tool
async def smart_home(context: RunContext, action: HomeAction, entity: str = "", value: str = "") -> str:
    """Control the smart home via Home Assistant: lights, sockets, AC/heating,
    curtains, TV, vacuum, scenes ("включи свет на кухне", "поставь 22 градуса",
    "запусти сцену кино"). Unlocking a door ("lock" + on) only after the user
    clearly asked for it.

    Args:
        action: list (devices and states) | state | on | off | toggle |
            brightness (value 0-100) | temperature (value °C) | scene (run a scene/script).
        entity: Device name as the user says it ("свет в спальне"), or an entity_id.
            For list: optional filter like "light" or "кухня".
        value: Number for brightness/temperature.
    """
    result = await _smart_home(action=action, entity=entity, value=value)
    return result["message"]
