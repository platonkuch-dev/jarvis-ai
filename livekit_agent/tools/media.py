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
