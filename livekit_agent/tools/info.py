"""Lookup tools: web search, weather (Open-Meteo, no API key needed), calculator.

`calculate` never calls `eval`/`exec` -- it walks a parsed AST and only allows
numeric literals plus a small whitelist of operators and math functions.
"""

from __future__ import annotations

import ast
import asyncio
import math
import operator

import httpx
from livekit.agents import RunContext, function_tool

import config
from tools._logging import log_call
from tools.registry import register_impl, register_tool

# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------


@register_impl("web_search")
@log_call("web_search")
async def _web_search(*, query: str) -> dict:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        return {"status": "error", "message": "Веб-поиск недоступен: не установлен duckduckgo_search."}

    def _search() -> list[dict]:
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=5))

    try:
        results = await asyncio.to_thread(_search)
    except Exception as exc:
        return {"status": "error", "message": f"Ошибка веб-поиска: {exc}"}

    if not results:
        return {"status": "not_found", "message": f"По запросу «{query}» ничего не найдено."}

    top = results[0]
    summary = top.get("body") or top.get("title") or ""
    message = f"{top.get('title', '')}: {summary[:280]}"
    return {"status": "ok", "message": message, "results": results}


@register_tool
@function_tool
async def web_search(context: RunContext, query: str) -> str:
    """Search the web and return a short summary of the top result.

    Args:
        query: The search query.
    """
    result = await _web_search(query=query)
    return result["message"]


# ---------------------------------------------------------------------------
# get_weather (Open-Meteo: free, no API key)
# ---------------------------------------------------------------------------


_WEATHER_CODES = {
    0: "ясно",
    1: "преимущественно ясно",
    2: "переменная облачность",
    3: "пасмурно",
    45: "туман",
    48: "изморозь",
    51: "лёгкая морось",
    53: "морось",
    55: "сильная морось",
    61: "небольшой дождь",
    63: "дождь",
    65: "сильный дождь",
    71: "небольшой снег",
    73: "снег",
    75: "сильный снегопад",
    80: "ливень",
    81: "сильный ливень",
    82: "очень сильный ливень",
    95: "гроза",
    96: "гроза с градом",
}


@register_impl("get_weather")
@log_call("get_weather")
async def _get_weather(*, location: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            geo_resp = await client.get(
                config.OPEN_METEO_GEOCODE_URL, params={"name": location, "count": 1, "language": "ru"}
            )
            geo_resp.raise_for_status()
            geo = geo_resp.json()
            candidates = geo.get("results") or []
            if not candidates:
                return {"status": "not_found", "message": f"Не нашёл город «{location}»."}

            place = candidates[0]
            forecast_resp = await client.get(
                config.OPEN_METEO_FORECAST_URL,
                params={
                    "latitude": place["latitude"],
                    "longitude": place["longitude"],
                    "current_weather": "true",
                },
            )
            forecast_resp.raise_for_status()
            current = forecast_resp.json().get("current_weather", {})
    except Exception as exc:
        return {"status": "error", "message": f"Не удалось получить погоду: {exc}"}

    if not current:
        return {"status": "error", "message": "Сервис погоды не вернул данные."}

    code = int(current.get("weathercode", -1))
    condition = _WEATHER_CODES.get(code, "неизвестные условия")
    temp = current.get("temperature")
    wind = current.get("windspeed")
    place_name = place.get("name", location)

    message = f"В городе {place_name}: {condition}, {temp}°C, ветер {wind} км/ч."
    return {"status": "ok", "message": message, "temperature": temp, "condition": condition}


@register_tool
@function_tool
async def get_weather(context: RunContext, location: str) -> str:
    """Get the current weather for a city.

    Args:
        location: City name, e.g. "Москва" or "Berlin".
    """
    result = await _get_weather(location=location)
    return result["message"]


# ---------------------------------------------------------------------------
# calculate (safe AST evaluator, no eval/exec)
# ---------------------------------------------------------------------------

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {
    "sqrt": math.sqrt,
    "abs": abs,
    "round": round,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "floor": math.floor,
    "ceil": math.ceil,
}
_CONSTS = {"pi": math.pi, "e": math.e}


class _UnsafeExpression(ValueError):
    pass


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise _UnsafeExpression("only numeric literals are allowed")
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            raise _UnsafeExpression("only whitelisted functions are allowed")
        if node.keywords:
            raise _UnsafeExpression("keyword arguments are not allowed")
        args = [_eval_node(a) for a in node.args]
        return _FUNCS[node.func.id](*args)
    if isinstance(node, ast.Name) and node.id in _CONSTS:
        return _CONSTS[node.id]
    raise _UnsafeExpression(f"disallowed expression: {ast.dump(node)}")


def safe_eval(expression: str) -> float:
    tree = ast.parse(expression, mode="eval")
    return _eval_node(tree)


@register_impl("calculate")
@log_call("calculate")
async def _calculate(*, expression: str) -> dict:
    try:
        value = safe_eval(expression)
    except (_UnsafeExpression, SyntaxError, ZeroDivisionError, TypeError, ValueError) as exc:
        return {"status": "error", "message": f"Не смог посчитать «{expression}»: {exc}"}

    return {"status": "ok", "message": f"{expression} = {value}", "value": value}


@register_tool
@function_tool
async def calculate(context: RunContext, expression: str) -> str:
    """Evaluate a math expression (arithmetic + sqrt/sin/cos/log/etc, no code execution).

    Args:
        expression: A math expression, e.g. "12 * (3 + 4) / 2" or "sqrt(2)".
    """
    result = await _calculate(expression=expression)
    return result["message"]
