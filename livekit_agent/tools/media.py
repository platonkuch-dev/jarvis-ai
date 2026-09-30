"""Media playback control via virtual media keys (Windows) plus system volume."""

from __future__ import annotations

import asyncio
from typing import Literal

from livekit.agents import RunContext, function_tool

import config
from tools._logging import log_call
from tools.registry import register_impl, register_tool
from tools.system import _set_volume

SYSTEM = config.SYSTEM

# Windows virtual key codes for media keys.
_VK_MEDIA_PLAY_PAUSE = 0xB3
_VK_MEDIA_NEXT_TRACK = 0xB0
_VK_MEDIA_PREV_TRACK = 0xB1


def _press_media_key(vk: int) -> None:
    import ctypes

    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)  # type: ignore[attr-defined]
    ctypes.windll.user32.keybd_event(vk, 0, 2, 0)  # type: ignore[attr-defined] # KEYEVENTF_KEYUP


MediaAction = Literal["play", "pause", "next", "prev", "volume"]


@register_impl("media_control")
@log_call("media_control")
async def _media_control(*, action: str, value: int | None = None) -> dict:
    if SYSTEM != "Windows" and action != "volume":
        return {"status": "error", "message": "Управление медиаклавишами реализовано только для Windows."}

    def _run() -> str:
        if action in ("play", "pause"):
            _press_media_key(_VK_MEDIA_PLAY_PAUSE)
            return "Переключил воспроизведение."
        if action == "next":
            _press_media_key(_VK_MEDIA_NEXT_TRACK)
            return "Следующий трек."
        if action == "prev":
            _press_media_key(_VK_MEDIA_PREV_TRACK)
            return "Предыдущий трек."
        if action == "volume":
            if value is None:
                return "Для управления громкостью нужно указать значение 0-100."
            return _set_volume(int(value))
        return "Неизвестное действие медиаплеера."

    try:
        message = await asyncio.to_thread(_run)
        return {"status": "ok", "message": message}
    except Exception as exc:
        return {"status": "error", "message": f"Не удалось выполнить команду плеера: {exc}"}


@register_tool
@function_tool
async def media_control(context: RunContext, action: MediaAction, value: int | None = None) -> str:
    """Control media playback: play/pause, skip track, or set volume.

    Args:
        action: One of "play", "pause", "next", "prev", "volume".
        value: Volume percentage 0-100, only used when action is "volume".
    """
    result = await _media_control(action=action, value=value)
    return result["message"]


# ---------------------------------------------------------------------------
# play_music: find a track/video by name and start it
# ---------------------------------------------------------------------------

MusicService = Literal["youtube", "spotify", "yandex"]


def _first_youtube_url(query: str) -> str | None:
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS  # type: ignore[no-redef]

    with DDGS() as ddgs:
        for r in ddgs.videos(query, max_results=8):
            url = r.get("content") or r.get("url") or ""
            if "youtube.com/watch" in url or "youtu.be/" in url:
                return url
    return None


def _open(target: str) -> None:
    import os
    import webbrowser

    if SYSTEM == "Windows" and not target.startswith("http"):
        os.startfile(target)  # type: ignore[attr-defined]  # spotify: URIs go to the app
    else:
        webbrowser.open(target)


@register_impl("play_music")
@log_call("play_music")
async def _play_music(*, query: str, service: str = "youtube") -> dict:
    from urllib.parse import quote

    query = query.strip()
    if not query:
        return {"status": "error", "message": "Скажите, что включить."}
    try:
        if service == "spotify":
            await asyncio.to_thread(_open, f"spotify:search:{quote(query)}")
            return {"status": "ok", "message": f"Открыл поиск «{query}» в Spotify."}
        if service == "yandex":
            await asyncio.to_thread(_open, f"https://music.yandex.ru/search?text={quote(query)}")
            return {"status": "ok", "message": f"Открыл «{query}» в Яндекс Музыке."}
        url = await asyncio.to_thread(_first_youtube_url, query)
        if url is None:
            await asyncio.to_thread(_open, f"https://www.youtube.com/results?search_query={quote(query)}")
            return {"status": "ok", "message": f"Точного видео не нашёл, открыл поиск «{query}» на YouTube."}
        await asyncio.to_thread(_open, url)
        return {"status": "ok", "message": f"Включаю «{query}» на YouTube.", "url": url}
    except Exception as exc:
        return {"status": "error", "message": f"Не удалось включить музыку: {exc}"}


@register_tool
@function_tool
async def play_music(context: RunContext, query: str, service: MusicService = "youtube") -> str:
    """Play a song, artist, playlist or video by name ("включи Imagine Dragons",
    "поставь lo-fi для работы"). YouTube starts the best match right away;
    Spotify / Yandex open the search for it. For pause/next use media_control.

    Args:
        query: What to play, e.g. "Rammstein Sonne" or "музыка для концентрации".
        service: "youtube" (default), "spotify" or "yandex" -- only if the user named it.
    """
    result = await _play_music(query=query, service=service)
    return result["message"]
