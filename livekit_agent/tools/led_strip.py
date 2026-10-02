"""Bluetooth LED strip driven by the "Lotus Lantern" phone app (ELK-BLEDOM
family: ELK-BLEDOM, ELK-BLEDOB, MELK, LEDBLE...), talked to directly over BLE
from the PC -- no phone, no cloud.

Protocol: 9-byte frames `7e .. .. .. .. .. .. .. ef` written to characteristic
fff3. The controller accepts ONE connection at a time, so the phone app must be
closed while Jarvis drives the strip, and Jarvis drops its connection after a
short idle period so the phone can take over again.

The strip's address comes from LED_STRIP_ADDRESS, else from the last successful
scan (cached in data/led_strip.json), else a fresh scan by name.
"""

from __future__ import annotations

import asyncio
import atexit
import re
import threading
from typing import Literal

from livekit.agents import RunContext, function_tool

import config
from tools._logging import log_call
from tools._store import read_json, write_json
from tools.registry import register_impl, register_tool

LedAction = Literal["on", "off", "color", "brightness", "effect", "speed", "scan"]

_CACHE_FILE = config.DATA_DIR / "led_strip.json"
_NAME_PREFIXES = ("ELK-", "MELK", "LEDBLE", "BLEDOM", "LEDDMX", "ELK_")
_SERVICE = "0000fff0-0000-1000-8000-00805f9b34fb"
_WRITE_CHAR = "0000fff3-0000-1000-8000-00805f9b34fb"
_IDLE_DISCONNECT_S = 20

COLORS: dict[str, tuple[int, int, int]] = {
    "красн": (255, 0, 0), "зелен": (0, 255, 0), "син": (0, 0, 255), "голуб": (0, 160, 255),
    "бирюз": (0, 255, 200), "желт": (255, 200, 0), "оранж": (255, 80, 0), "фиолет": (140, 0, 255),
    "пурпур": (200, 0, 255), "розов": (255, 30, 120), "малин": (255, 0, 80), "бел": (255, 255, 255),
    "тепл": (255, 140, 40), "холод": (180, 210, 255), "лимон": (200, 255, 0), "лаванд": (170, 120, 255),
    "red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255), "white": (255, 255, 255),
    "yellow": (255, 200, 0), "orange": (255, 80, 0), "purple": (140, 0, 255), "pink": (255, 30, 120),
    "cyan": (0, 255, 255),
}

# Built-in modes of the controller (byte after "7e 00 03").
EFFECTS: dict[str, int] = {
    "радуга": 0x87, "переливы": 0x87, "плавно": 0x87, "плавный": 0x87, "fade": 0x87,
    "прыжки": 0x80, "jump": 0x80, "вспышки": 0x8B, "мигание": 0x8B, "strobe": 0x8B,
    "дыхание красный": 0x8C, "дыхание зеленый": 0x8D, "дыхание синий": 0x8E, "дыхание": 0x8C,
}

_client = None
_lock = asyncio.Lock()
# A command may never hold anything up for long: at the strip's usual spot the
# signal is weak (~-85 dBm) and bleak can spend minutes reconnecting. Past this
# it gives up and says so. And only the newest command matters -- "выключи"
# said three times while the first is still connecting runs once, not three
# times in a row.
_SEND_TIMEOUT_S = 15.0
_latest = 0
_idle_task: asyncio.Task | None = None
_ble_loop: asyncio.AbstractEventLoop | None = None


def _in_ble_thread(coro):
    """Run `coro` on a private event-loop thread. pywin32/pywinauto elsewhere in
    Jarvis leave the main thread in COM STA mode, where WinRT Bluetooth
    callbacks never arrive; a fresh thread is untouched by that."""
    global _ble_loop
    if _ble_loop is None:
        _ble_loop = asyncio.new_event_loop()
        threading.Thread(target=_ble_loop.run_forever, name="led-strip-ble", daemon=True).start()
    return asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, _ble_loop))


@atexit.register
def _disconnect_on_exit() -> None:
    # A link left open by a dead process keeps the controller "busy" for a while.
    if _ble_loop is not None and _client is not None:
        try:
            asyncio.run_coroutine_threadsafe(_client.disconnect(), _ble_loop).result(timeout=3)
        except Exception:
            pass


def parse_color(text: str) -> tuple[int, int, int] | None:
    t = text.strip().lower().replace("ё", "е")
    m = re.fullmatch(r"#?([0-9a-f]{6})", t)
    if m:
        h = m.group(1)
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    m = re.fullmatch(r"(\d{1,3})[ ,;]+(\d{1,3})[ ,;]+(\d{1,3})", t)
    if m:
        return tuple(max(0, min(255, int(v))) for v in m.groups())  # type: ignore[return-value]
    for key, rgb in COLORS.items():
        if key in t:
            return rgb
    return None


def _frames(action: str, value: str) -> list[bytes] | str:
    """Frames for an action, or an error message."""
    if action == "on":
        return [bytes([0x7E, 0x00, 0x04, 0xF0, 0x00, 0x01, 0xFF, 0x00, 0xEF])]
    if action == "off":
        return [bytes([0x7E, 0x00, 0x04, 0x00, 0x00, 0x00, 0xFF, 0x00, 0xEF])]
    if action == "color":
        rgb = parse_color(value)
        if rgb is None:
            return f"Не знаю цвет «{value}»."
        r, g, b = rgb
        return [bytes([0x7E, 0x00, 0x04, 0xF0, 0x00, 0x01, 0xFF, 0x00, 0xEF]),
                bytes([0x7E, 0x00, 0x05, 0x03, r, g, b, 0x00, 0xEF])]
    if action in ("brightness", "speed"):
        try:
            pct = max(0, min(100, int(float(str(value).rstrip("%")))))
        except ValueError:
            return "Нужно число от 0 до 100."
        cmd = 0x01 if action == "brightness" else 0x02
        return [bytes([0x7E, 0x00, cmd, pct, 0x00, 0x00, 0x00, 0x00, 0xEF])]
    if action == "effect":
        v = value.strip().lower().replace("ё", "е")
        code = next((c for k, c in EFFECTS.items() if k in v), None)
        if code is None:
            return "Режимы: " + ", ".join(sorted({k for k in EFFECTS if " " not in k and k.isalpha()})) + "."
        return [bytes([0x7E, 0x00, 0x04, 0xF0, 0x00, 0x01, 0xFF, 0x00, 0xEF]),
                bytes([0x7E, 0x00, 0x03, code, 0x03, 0x00, 0x00, 0x00, 0xEF])]
    return f"Неизвестное действие «{action}»."


async def _scan() -> list[tuple[str, str, int]]:
    from bleak import BleakScanner

    found = await BleakScanner.discover(timeout=8, return_adv=True)
    out = []
    for dev, adv in found.values():
        name = (adv.local_name or dev.name or "").strip()
        uuids = [u.lower() for u in adv.service_uuids]
        if name.upper().startswith(_NAME_PREFIXES) or _SERVICE in uuids:
            out.append((dev.address, name, adv.rssi))
    out.sort(key=lambda t: t[2], reverse=True)
    return out


async def _address() -> str | None:
    if config.LED_STRIP_ADDRESS:
        return config.LED_STRIP_ADDRESS
    cached = read_json(_CACHE_FILE, {}).get("address")
    if cached:
        return cached
    found = await _scan()
    if not found:
        return None
    write_json(_CACHE_FILE, {"address": found[0][0], "name": found[0][1]})
    return found[0][0]


async def _disconnect_later() -> None:
    global _client
    await asyncio.sleep(_IDLE_DISCONNECT_S)
    async with _lock:
        if _client is not None:
            try:
                await _client.disconnect()
            except Exception:
                pass
            _client = None


async def _connect():
    from bleak import BleakClient

    address = await _address()
    if address is None:
        raise LookupError("not_found")
    last: Exception | None = None
    for attempt in range(2):
        # Cached GATT table: the controller never changes it, and a fresh
        # discovery is where flaky connections usually hang.
        client = BleakClient(address, timeout=12, winrt={"use_cached_services": attempt == 0})
        try:
            await client.connect()
            return client
        except Exception as exc:
            last = exc
            try:
                await client.disconnect()  # never leave a half-open session behind
            except Exception:
                pass
            await asyncio.sleep(1)
    assert last is not None
    raise last


class _Superseded(Exception):
    pass


async def _send(frames: list[bytes], gen: int = 0) -> None:
    global _client, _idle_task

    async with _lock:
        if gen and gen != _latest:
            raise _Superseded()
        for attempt in range(2):
            if _client is None or not _client.is_connected:
                _client = await _connect()
            char = _client.services.get_characteristic(_WRITE_CHAR)
            if char is None:  # odd firmware: first writable characteristic of fff0
                svc = _client.services.get_service(_SERVICE)
                char = next((c for c in (svc.characteristics if svc else [])
                             if "write" in c.properties or "write-without-response" in c.properties), None)
                if char is None:
                    raise LookupError("no_char")
            try:
                for frame in frames:
                    await _client.write_gatt_char(char, frame, response=False)
                    await asyncio.sleep(0.05)
                break
            except Exception:
                # Link dropped since the last command: reconnect once.
                try:
                    await _client.disconnect()
                except Exception:
                    pass
                _client = None
                if attempt:
                    raise
    if _idle_task is not None:
        _idle_task.cancel()
    _idle_task = asyncio.create_task(_disconnect_later())


@register_impl("led_strip")
@log_call("led_strip")
async def _led_strip(*, action: str, value: str = "") -> dict:
    try:
        import bleak  # noqa: F401
    except ImportError:
        return {"status": "error", "message": "Не установлен модуль bleak (pip install bleak)."}

    if action == "scan":
        try:
            found = await _in_ble_thread(_scan())
        except Exception as exc:
            return {"status": "error", "message": f"Bluetooth недоступен: {exc}"}
        if not found:
            return {"status": "not_found", "message": (
                "Ленту не нашёл. Закройте Lotus Lantern на телефоне (лента держит одно подключение) "
                "и проверьте, что Bluetooth на компьютере включён.")}
        write_json(_CACHE_FILE, {"address": found[0][0], "name": found[0][1]})
        return {"status": "ok", "message": "Нашёл: " + "; ".join(f"{n or 'без имени'} ({a})" for a, n, _ in found) + "."}

    frames = _frames(action, value)
    if isinstance(frames, str):
        return {"status": "error", "message": frames}
    global _latest
    _latest += 1
    gen = _latest
    try:
        await _in_ble_thread(asyncio.wait_for(_send(frames, gen), _SEND_TIMEOUT_S))
    except _Superseded:
        return {"status": "ok", "message": "Эту команду перебила более новая."}
    except asyncio.TimeoutError:
        return {"status": "error", "message": (
            "Лента не ответила за 15 секунд — скорее всего, она далеко от компьютера и сигнал слабый.")}
    except LookupError as exc:
        if str(exc) == "no_char":
            return {"status": "error", "message": "Устройство найдено, но это не лента Lotus Lantern."}
        return {"status": "not_found", "message": (
            "Ленту не нашёл. Закройте Lotus Lantern на телефоне и проверьте Bluetooth на компьютере.")}
    except Exception as exc:
        return {"status": "error", "message": (
            f"Не удалось подключиться к ленте ({type(exc).__name__}). "
            "Либо лента далеко от компьютера (слабый сигнал), либо её держит телефон с Lotus Lantern.")}

    done = {"on": "Лента включена.", "off": "Лента выключена.", "color": "Цвет ленты поменял.",
            "brightness": f"Яркость ленты {value}%.", "speed": f"Скорость эффекта {value}%.",
            "effect": f"Режим ленты: {value}."}[action]
    return {"status": "ok", "message": done}


@register_tool
@function_tool
async def led_strip(context: RunContext, action: LedAction, value: str = "") -> str:
    """Control the user's Bluetooth LED strip (the one from the "Lotus Lantern"
    app): "включи ленту", "сделай подсветку синей", "яркость ленты 30",
    "режим радуга". Not for Home Assistant lights -- that's smart_home.

    Args:
        action: on | off | color (value: color name like "синий", "#ff8800" or "255,0,0") |
            brightness (value 0-100) | effect (value: радуга, прыжки, вспышки, дыхание) |
            speed (effect speed 0-100) | scan (find the strip over Bluetooth).
        value: See action.
    """
    result = await _led_strip(action=action, value=value)
    return result["message"]
