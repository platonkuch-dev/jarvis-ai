"""Lets Jarvis switch its own ElevenLabs TTS voice on request, live -- no
restart needed for the current session, and it persists for future ones.

Only meaningful when TTS_PROVIDER=elevenlabs (config.py); edge-tts has its
own separate voice knobs (TTS_VOICE/TTS_VOICE_ALT env vars) that aren't
switchable at runtime the same way, since it's a per-utterance parameter
rather than a stateful client object with an update_options() call.
"""

from __future__ import annotations

import json

from livekit.agents import RunContext, function_tool

import config
from tools import runtime
from tools._logging import log_call
from tools.registry import register_impl, register_tool


def _persist_voice(voice_id: str, voice_name: str) -> None:
    config.TTS_VOICE_STATE_FILE.write_text(
        json.dumps({"voice_id": voice_id, "voice_name": voice_name}, ensure_ascii=False),
        encoding="utf-8",
    )


@register_impl("change_voice")
@log_call("change_voice")
async def _change_voice(*, voice_name: str) -> dict:
    if config.TTS_PROVIDER != "elevenlabs":
        return {
            "status": "error",
            "message": "Смена голоса сейчас доступна только для голосового движка ElevenLabs.",
        }

    key = voice_name.strip().lower()
    presets = config.load_voice_presets()
    voice_id = presets.get(key)
    if voice_id is None:
        if not presets:
            return {
                "status": "error",
                "message": "Сохранённых голосов пока нет — добавьте их в панели управления (раздел «Голоса»).",
            }
        return {
            "status": "error",
            "message": f"Не знаю голос «{voice_name}». Доступные: {', '.join(presets)}.",
        }

    session = runtime.get_active_session()
    if session is not None and session.tts is not None:
        try:
            session.tts.update_options(voice_id=voice_id)
        except Exception as exc:
            return {"status": "error", "message": f"Не удалось переключить голос: {exc}"}

    _persist_voice(voice_id, key)
    return {"status": "ok", "message": f"Голос переключён на «{voice_name}»."}


@register_tool
@function_tool
async def change_voice(context: RunContext, voice_name: str) -> str:
    """Switches Jarvis's own speaking voice (ElevenLabs) to a named preset,
    live -- takes effect on the very next reply, no restart needed.

    Args:
        voice_name: One of the saved voice preset names (the user adds them in
            the control panel), exactly as the user said it.
    """
    result = await _change_voice(voice_name=voice_name)
    return result["message"]


@register_impl("set_microphone")
@log_call("set_microphone")
async def _set_microphone(*, on: bool) -> dict:
    import mic_control

    mic_control.set_muted(not on)
    if on:
        return {"status": "ok", "message": "Включаю микрофон."}
    return {"status": "ok", "message": "Выключаю микрофон. Включить — F9 или кнопкой на панели.",
            "speech": "Выключаю микрофон."}


@register_tool
@function_tool
async def set_microphone(context: RunContext, on: bool) -> str:
    """Turn Jarvis's microphone off or on ("выключи микрофон", "не слушай меня";
    from Telegram also "включи микрофон"). While it's off he hears nothing, not
    even "Hey Jarvis" -- it comes back with F9 or the panel's mic button.

    Args:
        on: False to mute, True to unmute.
    """
    result = await _set_microphone(on=on)
    return result["message"]
