"""Lookup tools: web search, weather + forecast (Open-Meteo, no API key needed),
exchange rates (fiat + crypto, keyless public APIs), calculator.

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
async def _web_search(*, query: str, max_results: int = 5) -> dict:
    try:
        from ddgs import DDGS
    except ImportError:
        try:  # the package's old name
            from duckduckgo_search import DDGS
        except ImportError:
            return {"status": "error", "message": "Веб-поиск недоступен: не установлен ddgs."}

    max_results = max(1, min(int(max_results), 10))

    def _search() -> list[dict]:
        with DDGS() as ddgs:
            return list(ddgs.text(query, region=config.WEB_SEARCH_REGION, max_results=max_results))

    try:
        results = await asyncio.to_thread(_search)
    except Exception as exc:
        return {"status": "error", "message": f"Ошибка веб-поиска: {exc}"}

    if not results:
        return {"status": "not_found", "message": f"По запросу «{query}» ничего не найдено."}

    lines = [
        f"{i}. {r.get('title', '')} — {(r.get('body') or '')[:300]} [{r.get('href', '')}]"
        for i, r in enumerate(results, 1)
    ]
    message = ("\n".join(lines) + "\n\nЕсли сниппетов мало для точного ответа — прочитай нужную "
               "страницу через read_webpage.")
    return {"status": "ok", "message": message, "results": results}


@register_tool
@function_tool
async def web_search(context: RunContext, query: str, max_results: int = 5) -> str:
    """Search the internet (DuckDuckGo): titles, snippets and links of the top results.
    For fresh facts (news, prices, rates, schedules, scores) follow up with
    read_webpage on the best link instead of guessing from the snippets.

    Args:
        query: The search query, in the user's language.
        max_results: How many results to return (1-10).
    """
    result = await _web_search(query=query, max_results=max_results)
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


_WEEKDAYS_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def _condition(code: object) -> str:
    try:
        return _WEATHER_CODES.get(int(code), "неизвестные условия")  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "неизвестные условия"


@register_impl("get_weather")
@log_call("get_weather")
async def _get_weather(*, location: str, days: int = 0) -> dict:
    days = max(0, min(int(days or 0), 7))
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
            params: dict = {
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current_weather": "true",
                "timezone": "auto",
            }
            if days:
                params["daily"] = "weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
                params["forecast_days"] = days
            forecast_resp = await client.get(config.OPEN_METEO_FORECAST_URL, params=params)
            forecast_resp.raise_for_status()
            data = forecast_resp.json()
            current = data.get("current_weather", {})
    except Exception as exc:
        return {"status": "error", "message": f"Не удалось получить погоду: {exc}"}

    if not current:
        return {"status": "error", "message": "Сервис погоды не вернул данные."}

    condition = _condition(current.get("weathercode", -1))
    temp = current.get("temperature")
    wind = current.get("windspeed")
    place_name = place.get("name", location)

    message = f"В городе {place_name}: {condition}, {temp}°C, ветер {wind} км/ч."
    daily = data.get("daily") or {}
    if days and daily.get("time"):
        from datetime import date

        rains = daily.get("precipitation_probability_max") or []
        parts = []
        for i, day in enumerate(daily["time"]):
            d = date.fromisoformat(day)
            rain = rains[i] if i < len(rains) else None
            rain_txt = f", осадки {rain}%" if rain is not None else ""
            label = f"{_WEEKDAYS_RU[d.weekday()]} {d.strftime('%d.%m')}: " if days > 1 else ""
            parts.append(
                f"{label}{_condition(daily['weathercode'][i])}, "
                f"{daily['temperature_2m_min'][i]:.0f}…{daily['temperature_2m_max'][i]:.0f}°C{rain_txt}"
            )
        message += (" Прогноз: " if days > 1 else " За день: ") + "; ".join(parts) + "."
    return {"status": "ok", "message": message, "temperature": temp, "condition": condition}


@register_tool
@function_tool
async def get_weather(context: RunContext, location: str, days: int = 0) -> str:
    """Get the current weather for a city, optionally with a daily forecast.

    Args:
        location: City name, e.g. "Москва" or "Berlin".
        days: 0 = only now; 1-7 = also a day-by-day forecast (1 = today,
            2 = today+tomorrow, 7 = the week).
    """
    result = await _get_weather(location=location, days=days)
    return result["message"]


# ---------------------------------------------------------------------------
# exchange_rate (fiat: open.er-api.com, crypto: CoinGecko -- both keyless)
# ---------------------------------------------------------------------------

_FIAT_ALIASES = {
    "доллар": "USD", "бакс": "USD", "usd": "USD", "евро": "EUR", "eur": "EUR", "рубл": "RUB", "rub": "RUB",
    "гривн": "UAH", "тенге": "KZT", "юан": "CNY", "фунт": "GBP", "иен": "JPY", "йен": "JPY",
    "франк": "CHF", "лир": "TRY", "злот": "PLN", "бел": "BYN", "дирхам": "AED", "лари": "GEL",
    "драм": "AMD", "сум": "UZS",
}
_CRYPTO_ALIASES = {
    "биткоин": "bitcoin", "биток": "bitcoin", "btc": "bitcoin", "bitcoin": "bitcoin",
    "эфир": "ethereum", "eth": "ethereum", "ethereum": "ethereum", "тон": "the-open-network",
    "ton": "the-open-network", "солан": "solana", "sol": "solana", "usdt": "tether", "тезер": "tether",
    "доги": "dogecoin", "doge": "dogecoin", "xrp": "ripple", "рипл": "ripple", "bnb": "binancecoin",
}


def _resolve(name: str, table: dict[str, str]) -> str | None:
    q = name.strip().lower()
    if q in table:
        return table[q]
    return next((v for k, v in table.items() if len(k) > 3 and q.startswith(k)), None)


def _money(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ")


@register_impl("exchange_rate")
@log_call("exchange_rate")
async def _exchange_rate(*, base: str, target: str = "RUB", amount: float = 1.0) -> dict:
    amount = float(amount or 1.0)
    crypto = _resolve(base, _CRYPTO_ALIASES)
    fiat_target = _resolve(target, _FIAT_ALIASES) or target.strip().upper()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if crypto:
                vs = fiat_target.lower()
                resp = await client.get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": crypto, "vs_currencies": vs, "include_24hr_change": "true"},
                )
                resp.raise_for_status()
                row = resp.json().get(crypto) or {}
                if vs not in row:
                    return {"status": "not_found", "message": f"Нет курса {base} к {fiat_target}."}
                change = row.get(f"{vs}_24h_change")
                change_txt = f", за сутки {change:+.1f}%" if change is not None else ""
                total = row[vs] * amount
                return {"status": "ok", "value": total,
                        "message": f"{amount:g} {base} = {_money(total)} {fiat_target}{change_txt}."}

            fiat_base = _resolve(base, _FIAT_ALIASES) or base.strip().upper()
            resp = await client.get(f"https://open.er-api.com/v6/latest/{fiat_base}")
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        return {"status": "error", "message": f"Не удалось получить курс: {exc}"}

    rate = (data.get("rates") or {}).get(fiat_target)
    if data.get("result") != "success" or rate is None:
        return {"status": "not_found", "message": f"Не знаю валюту «{base}» или «{target}»."}
    total = rate * amount
    return {"status": "ok", "value": total,
            "message": f"{amount:g} {fiat_base} = {_money(total)} {fiat_target}."}


@register_tool
@function_tool
async def exchange_rate(context: RunContext, base: str, target: str = "RUB", amount: float = 1.0) -> str:
    """Current exchange rate / conversion for currencies and crypto (BTC, ETH,
    TON, SOL, USDT...). Informational only -- never give investment advice.

    Args:
        base: What to convert: currency code or name ("USD", "евро", "биткоин").
        target: Currency to express it in, default "RUB".
        amount: How many units of `base`, default 1.
    """
    result = await _exchange_rate(base=base, target=target, amount=amount)
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
