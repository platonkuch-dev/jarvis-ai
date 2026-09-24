"""Jarvis control panel: first-run setup, capability overview, voices, status.

    python panel.py            # starts the panel and opens it in the browser
    python panel.py --no-browser

Local only: binds 127.0.0.1, rejects any Host header that isn't the panel's
own address (DNS-rebinding), and every /api call needs the per-install token
kept in data/panel_token.txt. The panel can write your .env and start/stop
Jarvis, so it must never be reachable from another machine or another
website -- do not change the bind address.

Secrets are never sent back to the browser: the settings endpoint returns a
masked tail only, and a blank secret field on save means "leave unchanged".
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

import httpx
import psutil
from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

import capabilities

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ENV_FILE = BASE_DIR / ".env"
ENV_EXAMPLE = BASE_DIR / ".env.example"
STATIC_DIR = BASE_DIR / "panel_static"
TOKEN_FILE = DATA_DIR / "panel_token.txt"
VOICE_PRESETS_FILE = DATA_DIR / "voice_presets.json"
VOICE_STATE_FILE = DATA_DIR / "tts_voice.json"
TELEGRAM_SESSION = DATA_DIR / "jarvis_telegram"
DEFAULT_PORT = 8765

DATA_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Settings schema: the only keys the panel is allowed to read or write.
# ---------------------------------------------------------------------------

FIELDS: list[dict[str, Any]] = [
    {"group": "claude", "key": "ANTHROPIC_API_KEY", "label": "API-ключ Claude", "secret": True, "required": True,
     "placeholder": "sk-ant-api03-…", "help": "Создаётся в console.anthropic.com → API Keys. Нужен положительный баланс."},
    {"group": "claude", "key": "ANTHROPIC_MODEL", "label": "Модель для разговора", "placeholder": "claude-haiku-4-5-20251001",
     "help": "Оставьте пустым — будет быстрая и недорогая Haiku."},
    {"group": "openai", "key": "OPENAI_API_KEY", "label": "API-ключ OpenAI", "secret": True,
     "placeholder": "sk-…", "help": "platform.openai.com → API keys. Нужен, только если ниже выбран OpenAI."},
    {"group": "openai", "key": "LLM_PROVIDER", "label": "Модель для разговора", "type": "select",
     "options": [["anthropic", "Claude (по умолчанию)"], ["openai", "GPT-6 (Sol/Luna)"]],
     "help": "Кто отвечает на ваши слова и вызывает инструменты."},
    {"group": "openai", "key": "OPENAI_MODEL", "label": "Модель GPT-6 для разговора", "placeholder": "gpt-6-luna",
     "help": "Оставьте пустым — будет быстрая gpt-6-luna. gpt-6-sol или gpt-6-astra — глубже, но медленнее."},
    {"group": "openai", "key": "COMPUTER_USE_PROVIDER", "label": "Модель для управления экраном", "type": "select",
     "options": [["anthropic", "Claude Sonnet (по умолчанию)"], ["openai", "GPT-6 Sol"]],
     "help": "Кто «смотрит» на экран и кликает в use_computer — независимо от модели разговора выше."},
    {"group": "openai", "key": "OPENAI_COMPUTER_USE_MODEL", "label": "Модель GPT-6 для экрана", "placeholder": "gpt-6-sol",
     "help": "Используется, только если выше выбран OpenAI для управления экраном."},
    {"group": "stt", "key": "DEEPGRAM_API_KEY", "label": "API-ключ Deepgram", "secret": True, "required": True,
     "placeholder": "…", "help": "console.deepgram.com → API Keys. Есть бесплатный стартовый кредит."},
    {"group": "stt", "key": "DEEPGRAM_LANGUAGE", "label": "Язык речи", "placeholder": "ru", "help": "ru, uk, en, …"},
    {"group": "tts", "key": "TTS_PROVIDER", "label": "Озвучка", "type": "select",
     "options": [["edge", "Edge TTS — бесплатно"], ["elevenlabs", "ElevenLabs — живее, платно"]],
     "help": "Edge работает без ключей. ElevenLabs даёт свои и клонированные голоса."},
    {"group": "tts", "key": "TTS_VOICE", "label": "Голос Edge TTS", "placeholder": "ru-RU-DmitryNeural",
     "help": "ru-RU-DmitryNeural (мужской) или ru-RU-SvetlanaNeural (женский)."},
    {"group": "tts", "key": "ELEVENLABS_API_KEY", "label": "API-ключ ElevenLabs", "secret": True,
     "placeholder": "…", "help": "elevenlabs.io → Profile → API Keys. Нужен, только если выбрана озвучка ElevenLabs."},
    {"group": "tts", "key": "ELEVENLABS_VOICE_ID", "label": "ID голоса по умолчанию", "placeholder": "необязательно",
     "help": "Больше голосов, переключаемых голосом, добавляется ниже, в разделе «Голоса»."},
    {"group": "telegram", "key": "TELEGRAM_API_ID", "label": "api_id", "placeholder": "12345678",
     "help": "my.telegram.org → API development tools → создать приложение."},
    {"group": "telegram", "key": "TELEGRAM_API_HASH", "label": "api_hash", "secret": True, "placeholder": "…",
     "help": "Там же. Никому не показывайте — это ключ к вашему приложению."},
    {"group": "telegram", "key": "TELEGRAM_PHONE", "label": "Номер аккаунта Джарвиса", "placeholder": "+380…",
     "help": "Отдельный аккаунт Telegram для Джарвиса, не ваш личный."},
    {"group": "livekit", "key": "LIVEKIT_URL", "label": "LiveKit URL", "placeholder": "wss://….livekit.cloud",
     "help": "cloud.livekit.io → ваш проект → Settings. Нужен только для телефонных звонков."},
    {"group": "livekit", "key": "LIVEKIT_API_KEY", "label": "API Key", "placeholder": "API…"},
    {"group": "livekit", "key": "LIVEKIT_API_SECRET", "label": "API Secret", "secret": True, "placeholder": "…"},
    {"group": "extra", "key": "WAKE_HOTKEY", "label": "Клавиша сна/пробуждения", "placeholder": "f10"},
    {"group": "extra", "key": "SLEEP_AFTER_SILENCE_S", "label": "Автосон через (секунд тишины)", "placeholder": "180"},
    {"group": "extra", "key": "AUDIO_INPUT_DEVICE", "label": "Микрофон (номер или часть названия)", "placeholder": "по умолчанию"},
    {"group": "extra", "key": "AUDIO_OUTPUT_DEVICE", "label": "Динамики (номер или часть названия)", "placeholder": "по умолчанию"},
    {"group": "extra", "key": "USE_CLAUDE_CLI", "label": "Фоновые задачи через подписку Claude Code", "type": "select",
     "options": [["0", "Нет — только API-ключ (рекомендуется)"], ["1", "Да — через локальный claude CLI"]],
     "help": "Только для памяти и анализа привычек. Убедитесь, что условия вашей подписки Anthropic это разрешают."},
]
FIELD_BY_KEY = {f["key"]: f for f in FIELDS}
SECRET_KEYS = {f["key"] for f in FIELDS if f.get("secret")}


def _mask(value: str) -> str:
    return "••••" + value[-4:] if len(value) > 8 else "••••"


def read_env() -> dict[str, str]:
    if not ENV_FILE.exists():
        return {}
    return {k: (v or "") for k, v in dotenv_values(ENV_FILE).items()}


def _quote(value: str) -> str:
    return f'"{value}"' if re.search(r"[\s#'\"]", value) else value


def write_env(updates: dict[str, str | None]) -> None:
    """Sets/removes keys in .env, keeping every other line and comment intact."""
    if not ENV_FILE.exists():
        if ENV_EXAMPLE.exists():
            shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
        else:
            ENV_FILE.write_text("", encoding="utf-8")
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        m = re.match(r"^\s*([A-Za-z0-9_]+)\s*=", line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            value = remaining.pop(key)
            if value is None:
                continue
            out.append(f"{key}={_quote(value)}")
        else:
            out.append(line)
    for key, value in remaining.items():
        if value is not None:
            out.append(f"{key}={_quote(value)}")
    tmp = ENV_FILE.with_suffix(".env.tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, ENV_FILE)


# ---------------------------------------------------------------------------
# Auth: local-only + per-install token
# ---------------------------------------------------------------------------


def _load_token() -> str:
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if len(token) >= 20:
            return token
    token = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    return token


TOKEN = _load_token()
PORT = DEFAULT_PORT

app = FastAPI(title="Jarvis control panel", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def _guard(request: Request, call_next):
    host = (request.headers.get("host") or "").lower()
    if host not in {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}:
        return JSONResponse({"detail": "bad host"}, status_code=403)
    if request.url.path.startswith("/api/"):
        supplied = request.headers.get("x-panel-token", "")
        if not hmac.compare_digest(supplied, TOKEN):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    env = read_env()
    fields = []
    for f in FIELDS:
        value = env.get(f["key"], "")
        item = {k: v for k, v in f.items()}
        item["is_set"] = bool(value)
        item["value"] = _mask(value) if (f.get("secret") and value) else value
        fields.append(item)
    return {"fields": fields, "env_exists": ENV_FILE.exists(), "platform": platform.system()}


@app.post("/api/config")
async def save_config(request: Request) -> dict[str, Any]:
    body = await request.json()
    values: dict[str, Any] = body.get("values") or {}
    clear: list[str] = body.get("clear") or []
    updates: dict[str, str | None] = {}
    for key, raw in values.items():
        if key not in FIELD_BY_KEY:
            raise HTTPException(400, f"Неизвестная настройка: {key}")
        value = str(raw).strip()
        if "\n" in value or "\r" in value:
            raise HTTPException(400, f"Недопустимое значение для {key}")
        if key in SECRET_KEYS and not value:
            continue  # blank secret = unchanged
        if key in SECRET_KEYS and value.startswith("••••"):
            continue  # the masked placeholder echoed back
        updates[key] = value if value else None
    for key in clear:
        if key in FIELD_BY_KEY:
            updates[key] = None
    if updates:
        write_env(updates)
    return {"ok": True, "saved": sorted(updates)}


# ---------------------------------------------------------------------------
# Connection tests
# ---------------------------------------------------------------------------


async def _test_anthropic(env: dict[str, str]) -> dict[str, Any]:
    key = env.get("ANTHROPIC_API_KEY", "")
    if not key:
        return {"ok": False, "message": "Ключ не указан."}
    model = env.get("ANTHROPIC_MODEL") or "claude-haiku-4-5-20251001"
    async with httpx.AsyncClient(timeout=20) as client:
        # A 1-token request (a fraction of a cent) rather than a free
        # "list models" call: it also proves the account has credit, which is
        # the failure that silently makes the assistant deaf and mute.
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]},
        )
    if r.status_code == 200:
        return {"ok": True, "message": f"Ключ работает, модель {model} отвечает."}
    detail = ""
    try:
        detail = r.json().get("error", {}).get("message", "")
    except ValueError:
        pass
    if r.status_code == 401:
        return {"ok": False, "message": "Ключ не принят (401). Проверьте, что скопировали его целиком."}
    if "credit balance" in detail.lower():
        return {"ok": False, "message": "Ключ верный, но на балансе нет средств. Пополните в console.anthropic.com → Billing."}
    if r.status_code == 404:
        return {"ok": False, "message": f"Модель «{model}» недоступна для этого ключа."}
    return {"ok": False, "message": f"Ошибка {r.status_code}: {detail[:200]}"}


async def _test_deepgram(env: dict[str, str]) -> dict[str, Any]:
    key = env.get("DEEPGRAM_API_KEY", "")
    if not key:
        return {"ok": False, "message": "Ключ не указан."}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get("https://api.deepgram.com/v1/projects", headers={"Authorization": f"Token {key}"})
    if r.status_code == 200:
        return {"ok": True, "message": "Ключ Deepgram работает."}
    if r.status_code in (401, 403):
        return {"ok": False, "message": "Ключ Deepgram не принят."}
    return {"ok": False, "message": f"Ошибка {r.status_code}."}


async def _test_elevenlabs(env: dict[str, str]) -> dict[str, Any]:
    key = env.get("ELEVENLABS_API_KEY", "")
    if not key:
        return {"ok": False, "message": "Ключ не указан."}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get("https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": key})
    if r.status_code == 200:
        return {"ok": True, "message": f"Ключ работает, голосов в аккаунте: {len(r.json().get('voices', []))}."}
    if r.status_code in (401, 403):
        return {"ok": False, "message": "Ключ ElevenLabs не принят (или у него нет права читать голоса)."}
    return {"ok": False, "message": f"Ошибка {r.status_code}."}


async def _test_openai(env: dict[str, str]) -> dict[str, Any]:
    key = env.get("OPENAI_API_KEY", "")
    if not key:
        return {"ok": False, "message": "Ключ не указан."}
    # Free GET (no token cost), same idea as the Deepgram/ElevenLabs checks
    # above: proves the key is accepted without spending anything.
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"})
    if r.status_code == 200:
        return {"ok": True, "message": "Ключ OpenAI работает."}
    if r.status_code == 401:
        return {"ok": False, "message": "Ключ не принят (401). Проверьте, что скопировали его целиком."}
    detail = ""
    try:
        detail = r.json().get("error", {}).get("message", "")
    except ValueError:
        pass
    return {"ok": False, "message": f"Ошибка {r.status_code}: {detail[:200]}"}


async def _test_livekit(env: dict[str, str]) -> dict[str, Any]:
    url, key, secret = env.get("LIVEKIT_URL", ""), env.get("LIVEKIT_API_KEY", ""), env.get("LIVEKIT_API_SECRET", "")
    if not (url and key and secret):
        return {"ok": False, "message": "Заполните URL, API Key и API Secret."}
    from livekit import api

    lk = api.LiveKitAPI(url, key, secret)
    try:
        await lk.room.list_rooms(api.ListRoomsRequest())
    except Exception as exc:
        return {"ok": False, "message": f"LiveKit не принял данные: {str(exc)[:200]}"}
    finally:
        await lk.aclose()
    return {"ok": True, "message": "LiveKit доступен, ключи верны."}


async def _test_telegram(env: dict[str, str]) -> dict[str, Any]:
    if not (env.get("TELEGRAM_API_ID") and env.get("TELEGRAM_API_HASH")):
        return {"ok": False, "message": "Заполните api_id и api_hash."}
    session_file = Path(str(TELEGRAM_SESSION) + ".session")
    if not session_file.exists():
        return {"ok": False, "message": "Вход в аккаунт ещё не выполнен — нажмите «Войти в Telegram»."}
    from telethon import TelegramClient

    # Works on a throwaway copy: the live session file may be open in the
    # Jarvis processes and SQLite doesn't like two writers.
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "check"
        shutil.copyfile(session_file, str(copy) + ".session")
        client = TelegramClient(str(copy), int(env["TELEGRAM_API_ID"]), env["TELEGRAM_API_HASH"])
        try:
            await client.connect()
            if not await client.is_user_authorized():
                return {"ok": False, "message": "Сессия недействительна — войдите заново."}
            me = await client.get_me()
        except Exception as exc:
            return {"ok": False, "message": f"Не удалось проверить: {str(exc)[:200]}"}
        finally:
            await client.disconnect()
    return {"ok": True, "message": f"Вход выполнен: {me.first_name} ({me.phone})."}


_TESTS = {
    "anthropic": _test_anthropic,
    "deepgram": _test_deepgram,
    "elevenlabs": _test_elevenlabs,
    "openai": _test_openai,
    "livekit": _test_livekit,
    "telegram": _test_telegram,
}


@app.post("/api/test/{service}")
async def test_service(service: str) -> dict[str, Any]:
    fn = _TESTS.get(service)
    if fn is None:
        raise HTTPException(404, "Неизвестный сервис")
    try:
        return await fn(read_env())
    except httpx.HTTPError as exc:
        return {"ok": False, "message": f"Нет соединения с сервисом: {exc}"}


# ---------------------------------------------------------------------------
# Telegram login (replaces running telegram_login.py in a terminal)
# ---------------------------------------------------------------------------

_tg: dict[str, Any] = {"client": None, "phone": None, "hash": None}


async def _tg_reset() -> None:
    client = _tg.get("client")
    if client is not None:
        try:
            await client.disconnect()
        except Exception:
            pass
    _tg.update(client=None, phone=None, hash=None)


@app.post("/api/telegram/send_code")
async def telegram_send_code(request: Request) -> dict[str, Any]:
    env = read_env()
    api_id, api_hash = env.get("TELEGRAM_API_ID", ""), env.get("TELEGRAM_API_HASH", "")
    phone = ((await request.json()).get("phone") or env.get("TELEGRAM_PHONE", "")).strip()
    if not (api_id.isdigit() and api_hash):
        raise HTTPException(400, "Сначала сохраните api_id и api_hash.")
    if not phone:
        raise HTTPException(400, "Укажите номер телефона аккаунта Джарвиса.")
    from telethon import TelegramClient

    await _tg_reset()
    client = TelegramClient(str(TELEGRAM_SESSION), int(api_id), api_hash)
    try:
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            await client.disconnect()
            return {"ok": True, "already": True, "message": f"Уже выполнен вход: {me.first_name} ({me.phone})."}
        sent = await client.send_code_request(phone)
    except Exception as exc:
        await client.disconnect()
        return {"ok": False, "message": f"Telegram не отправил код: {str(exc)[:200]}"}
    _tg.update(client=client, phone=phone, hash=sent.phone_code_hash)
    write_env({"TELEGRAM_PHONE": phone})
    return {"ok": True, "message": "Код отправлен — он придёт в приложение Telegram (или SMS) на этот номер."}


@app.post("/api/telegram/sign_in")
async def telegram_sign_in(request: Request) -> dict[str, Any]:
    from telethon.errors import PhoneCodeExpiredError, PhoneCodeInvalidError, SessionPasswordNeededError

    body = await request.json()
    code, password = (body.get("code") or "").strip(), body.get("password") or ""
    client = _tg.get("client")
    if client is None:
        raise HTTPException(400, "Сначала запросите код.")
    try:
        try:
            await client.sign_in(_tg["phone"], code, phone_code_hash=_tg["hash"])
        except SessionPasswordNeededError:
            if not password:
                return {"ok": False, "needs_password": True,
                        "message": "На аккаунте включена двухфакторная защита — введите пароль."}
            await client.sign_in(password=password)
        me = await client.get_me()
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        return {"ok": False, "message": "Код неверный или устарел — запросите новый."}
    except Exception as exc:
        return {"ok": False, "message": f"Не удалось войти: {str(exc)[:200]}"}
    await _tg_reset()
    # The chat bridge and monitor keep their own copies of the session,
    # bootstrapped only when missing -- a stale copy from another account
    # would silently keep using it, so drop them (best effort: a running
    # process may hold the file, in which case it's the same account anyway).
    for suffix in ("_bridge", "_monitor"):
        try:
            Path(str(TELEGRAM_SESSION) + suffix + ".session").unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True, "message": f"Вход выполнен: {me.first_name} ({me.phone}). Перезапустите Джарвиса."}


# ---------------------------------------------------------------------------
# Voices
# ---------------------------------------------------------------------------


def _read_presets() -> dict[str, str]:
    try:
        raw = json.loads(VOICE_PRESETS_FILE.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


@app.get("/api/voices")
def get_voices() -> dict[str, Any]:
    env = read_env()
    current = None
    try:
        current = json.loads(VOICE_STATE_FILE.read_text(encoding="utf-8")).get("voice_id")
    except (OSError, ValueError):
        pass
    return {
        "provider": env.get("TTS_PROVIDER") or "edge",
        "presets": _read_presets(),
        "current_voice_id": current or env.get("ELEVENLABS_VOICE_ID") or None,
    }


@app.post("/api/voices")
async def add_voice(request: Request) -> dict[str, Any]:
    body = await request.json()
    name = (body.get("name") or "").strip().lower()
    voice_id = (body.get("voice_id") or "").strip()
    if not name or not re.fullmatch(r"[A-Za-z0-9]{10,40}", voice_id):
        raise HTTPException(400, "Нужно имя и ID голоса из ElevenLabs (латиница и цифры).")
    key = read_env().get("ELEVENLABS_API_KEY", "")
    label = None
    if key:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"https://api.elevenlabs.io/v1/voices/{voice_id}", headers={"xi-api-key": key})
        if r.status_code != 200:
            raise HTTPException(400, "ElevenLabs не нашёл такой голос в вашем аккаунте — проверьте ID.")
        label = r.json().get("name")
    presets = _read_presets()
    presets[name] = voice_id
    VOICE_PRESETS_FILE.write_text(json.dumps(presets, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "message": f"Голос «{name}» сохранён" + (f" (в ElevenLabs он называется «{label}»)." if label else ".")}


@app.delete("/api/voices/{name}")
def delete_voice(name: str) -> dict[str, Any]:
    presets = _read_presets()
    if presets.pop(name.strip().lower(), None) is None:
        raise HTTPException(404, "Нет такого голоса")
    VOICE_PRESETS_FILE.write_text(json.dumps(presets, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Camera picker
#
# hud_bar.py's CameraPanel is the only thing that actually holds a QCamera
# (see camera_bridge.py's docstring for why), so listing devices here means
# asking that already-running process over the same file bridge tools/
# camera.py uses for open/capture/close, then waiting briefly for the reply.
# If the HUD isn't running, this fails soft (empty list + hud_running: False)
# rather than importing PyQt into the panel process just to enumerate devices.
# ---------------------------------------------------------------------------

CAMERA_LIST_TIMEOUT_S = 3.0


def _camera_next_seq() -> int:
    # Nanosecond timestamp, not a small counter -- see tools/camera.py's
    # _next_seq() docstring: this is a second, independent writer to the same
    # camera_command.json/camera_result.json files, so seq values from the
    # two processes must not collide.
    return time.time_ns()


async def _camera_wait_result(seq: int, timeout: float) -> dict[str, Any] | None:
    import camera_bridge

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = camera_bridge.read_result()
        if result is not None and result.get("seq") == seq:
            return result
        await asyncio.sleep(0.1)
    return None


@app.get("/api/camera/devices")
async def list_camera_devices() -> dict[str, Any]:
    import camera_bridge

    seq = _camera_next_seq()
    camera_bridge.write_command(seq, "list_devices")
    result = await _camera_wait_result(seq, CAMERA_LIST_TIMEOUT_S)
    current = camera_bridge.read_preferred_device_id()
    if result is None:
        return {"devices": [], "current_device_id": current, "hud_running": False}
    return {"devices": result.get("devices") or [], "current_device_id": current, "hud_running": True}


@app.post("/api/camera/select")
async def select_camera_device(request: Request) -> dict[str, Any]:
    import camera_bridge

    body = await request.json()
    device_id = (body.get("device_id") or "").strip()
    description = (body.get("description") or "").strip()
    if device_id:
        camera_bridge.write_preferred_device(device_id, description)
        message = f"Камера «{description or device_id}» выбрана."
    else:
        camera_bridge.clear_preferred_device()
        message = "Выбрана камера по умолчанию (первая найденная)."
    return {"ok": True, "message": message}


# ---------------------------------------------------------------------------
# Status, capabilities, start/stop
# ---------------------------------------------------------------------------

_COMPONENTS = {
    "app": ("app.py",),
    "voice": ("worker.py", "console"),
    "phone": ("worker.py", "start"),
    "hud": ("hud_bar.py",),
    "telegram": ("telegram_bridge.py",),
    "monitor": ("proactive_monitor.py",),
}
_COMPONENT_LABELS = {
    "app": "Трей-приложение",
    "voice": "Голосовой диалог",
    "phone": "Телефон (LiveKit)",
    "hud": "Плашка статуса",
    "telegram": "Чат в Telegram",
    "monitor": "Мониторинг",
}


def _running() -> dict[str, list[psutil.Process]]:
    found: dict[str, list[psutil.Process]] = {k: [] for k in _COMPONENTS}
    base = str(BASE_DIR).lower()
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmd = [c.lower() for c in (proc.info["cmdline"] or [])]
        except (psutil.Error, OSError):
            continue
        joined = " ".join(cmd)
        if not cmd or base not in joined:
            continue
        for name, needles in _COMPONENTS.items():
            if all(n in joined for n in needles):
                found[name].append(proc)
    return found


def _adobe_found(product: str) -> bool:
    if platform.system() != "Windows":
        return False
    roots = [Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Adobe"]
    return any(list(root.glob(f"Adobe {product}*")) for root in roots if root.exists())


def _telegram_logged_in() -> bool:
    return Path(str(TELEGRAM_SESSION) + ".session").exists()


def _requirement(req: str, env: dict[str, str]) -> tuple[str, str]:
    """-> (state, reason); state is "ok" | "todo" | "na"."""
    if req == "windows":
        return ("ok", "") if platform.system() == "Windows" else ("na", "только Windows")
    if req == "anthropic":
        return ("ok", "") if env.get("ANTHROPIC_API_KEY") else ("todo", "ключ Claude")
    if req == "deepgram":
        return ("ok", "") if env.get("DEEPGRAM_API_KEY") else ("todo", "ключ Deepgram")
    if req == "elevenlabs":
        ok = env.get("TTS_PROVIDER") == "elevenlabs" and env.get("ELEVENLABS_API_KEY")
        return ("ok", "") if ok else ("todo", "озвучка ElevenLabs")
    if req == "telegram":
        if env.get("TELEGRAM_API_ID") and env.get("TELEGRAM_API_HASH") and _telegram_logged_in():
            return ("ok", "")
        return ("todo", "Telegram (ключи и вход)")
    if req == "livekit":
        ok = all(env.get(k) for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"))
        return ("ok", "") if ok else ("todo", "LiveKit")
    if req in ("adobe_ae", "adobe_ps", "adobe_pr"):
        product = {"adobe_ae": "After Effects", "adobe_ps": "Photoshop", "adobe_pr": "Premiere Pro"}[req]
        return ("ok", "") if _adobe_found(product) else ("na", f"{product} не найден на этом ПК")
    if req == "claude_code":
        missing = [n for n, exe in (("Claude Code", "claude"), ("VS Code", "code")) if not shutil.which(exe)]
        return ("ok", "") if not missing else ("na", "не найдено: " + ", ".join(missing))
    return ("ok", "")


def _item_status(item: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    todo, na = [], []
    for req in item.get("requires", []):
        state, reason = _requirement(req, env)
        if state == "todo":
            todo.append(reason)
        elif state == "na":
            na.append(reason)
    if na:
        return {"state": "na", "note": "; ".join(na)}
    if todo:
        return {"state": "todo", "note": "нужно настроить: " + ", ".join(todo)}
    return {"state": "ok", "note": ""}


_tools_cache: list[str] | None = None


def _registered_tools() -> list[str] | None:
    global _tools_cache
    if _tools_cache is None:
        try:
            import tools

            _tools_cache = sorted(getattr(t, "__name__", str(t)) for t in tools.FUNCTION_TOOLS)
        except Exception:
            return None
    return _tools_cache


@app.get("/api/capabilities")
async def get_capabilities() -> dict[str, Any]:
    env = read_env()
    registered = await asyncio.to_thread(_registered_tools)
    categories = []
    for cat in capabilities.CATEGORIES:
        items = [{**item, **_item_status(item, env)} for item in cat["items"]]
        categories.append({"id": cat["id"], "title": cat["title"], "items": items})
    background = [{**b, **_item_status(b, env)} for b in capabilities.BACKGROUND]
    return {
        "categories": categories,
        "background": background,
        "safety": capabilities.SAFETY,
        "tool_count": len(registered) if registered is not None else None,
        "unlisted_tools": capabilities.unlisted_tools(registered) if registered is not None else [],
    }


@app.get("/api/status")
def get_status() -> dict[str, Any]:
    env = read_env()
    running = _running()
    components = [
        {"id": cid, "label": _COMPONENT_LABELS[cid], "running": bool(procs)} for cid, procs in running.items()
    ]
    checks = [
        {"id": "anthropic", "label": "Claude", "required": True, **_check(_requirement("anthropic", env))},
        {"id": "deepgram", "label": "Распознавание речи (Deepgram)", "required": True, **_check(_requirement("deepgram", env))},
        {"id": "tts", "label": "Озвучка", "required": False,
         "ok": (env.get("TTS_PROVIDER") or "edge") == "edge" or bool(env.get("ELEVENLABS_API_KEY")),
         "note": "" if (env.get("TTS_PROVIDER") or "edge") == "edge" or env.get("ELEVENLABS_API_KEY") else "выбран ElevenLabs, но ключа нет"},
        {"id": "telegram", "label": "Telegram", "required": False, **_check(_requirement("telegram", env))},
        {"id": "livekit", "label": "Телефон (LiveKit)", "required": False, **_check(_requirement("livekit", env))},
    ]
    ready = all(c["ok"] for c in checks if c["required"])
    return {"components": components, "checks": checks, "ready_to_start": ready, "running": bool(running["app"])}


def _check(state_reason: tuple[str, str]) -> dict[str, Any]:
    state, reason = state_reason
    return {"ok": state == "ok", "note": "" if state == "ok" else reason}


@app.post("/api/start")
def start_jarvis() -> dict[str, Any]:
    if _running()["app"]:
        return {"ok": True, "message": "Джарвис уже запущен (значок в трее)."}
    env = read_env()
    if not (env.get("ANTHROPIC_API_KEY") and env.get("DEEPGRAM_API_KEY")):
        raise HTTPException(400, "Сначала укажите ключи Claude и Deepgram.")
    venv_py = BASE_DIR / ".venv" / "Scripts" / "pythonw.exe"
    sibling = Path(sys.executable).with_name("pythonw.exe")  # the installed app's bundled runtime
    exe = str(venv_py if venv_py.exists() else sibling if sibling.exists() else sys.executable)
    flags = 0
    if platform.system() == "Windows":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    subprocess.Popen(
        [exe, str(BASE_DIR / "app.py")], cwd=str(BASE_DIR), creationflags=flags,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return {"ok": True, "message": "Запускаю. Через несколько секунд в трее появится значок Джарвиса."}


@app.post("/api/stop")
def stop_jarvis() -> dict[str, Any]:
    stopped = 0
    for name, procs in _running().items():
        for proc in procs:
            try:
                proc.terminate()
                stopped += 1
            except psutil.Error:
                pass
    return {"ok": True, "message": "Остановлено." if stopped else "Джарвис и не был запущен."}


# ---------------------------------------------------------------------------


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def panel_url(port: int = DEFAULT_PORT) -> str:
    return f"http://127.0.0.1:{port}/?t={TOKEN}"


def _ensure_std_streams() -> None:
    """Started from a shortcut (pythonw.exe) there is no console, so sys.stdout/sys.stderr are None and
    uvicorn's logging setup crashes on the first `sys.stderr.isatty()`. Give them a log file instead."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    logs = BASE_DIR / "logs"
    logs.mkdir(exist_ok=True)
    stream = open(logs / "panel.log", "a", buffering=1, encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def main() -> None:
    global PORT
    _ensure_std_streams()
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    PORT = args.port

    if _port_in_use(PORT):
        # Already running (e.g. started by the tray app): just open it.
        if not args.no_browser:
            webbrowser.open(panel_url(PORT))
        return

    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(panel_url(PORT))).start()
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
