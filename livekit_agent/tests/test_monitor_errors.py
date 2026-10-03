"""proactive_monitor's app.log scan: real errors only, and never the
conversation text that sits next to them in the log."""

from __future__ import annotations

import asyncio

import config
import proactive_monitor as pm

CHAT_ONLY = """\
                      {"room": "console"}
    23:56:02.744 DEBUG    livekit.agents     conversation_item_added
                                         {"role": "assistant", "lk.pii.text":
"Обсудили планы на выходные, пожелал спокойной ночи.", "room": "console"}
"""

REAL_ERROR = """\
    23:51:52.450 ERROR    livekit.agents     Error in _tts_inference_task
                                         {"room": "console"}
Traceback (most recent call last):
  File "C:\\app\\worker.py", line 137, in tts_node
    raise APIConnectionError(
livekit.agents._exceptions.APIConnectionError: all TTSs failed
    23:51:52.464 DEBUG    livekit.agents     conversation_item_added
                                         {"role": "assistant", "lk.pii.text":
"Снова на связи.", "room": "console"}
"""

SELF_HEALING = """\
    23:51:52.859 ERROR    livekit.…levenlabs elevenlabs tts returned error
                                           {"context_id": null, "error":
"voice_id_does_not_exist", "lk.pii.data": {"message": "A voice does not exist."}}
    00:09:33.888 ERROR    livekit.….deepgram Error in recv_task
livekit.agents._exceptions.APIStatusError: message='deepgram connection closed', retryable=True
"""


def _scan(tmp_path, monkeypatch, text: str) -> str | None:
    log = tmp_path / "app.log"
    log.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(pm, "_log_offset", None)
    pm._check_recent_errors()                     # first look only records the end of the file
    log.write_text(text, encoding="utf-8")
    return pm._check_recent_errors()


def test_chat_text_alone_is_not_an_error(tmp_path, monkeypatch):
    assert _scan(tmp_path, monkeypatch, CHAT_ONLY) is None


def test_real_error_is_reported_without_conversation_text(tmp_path, monkeypatch):
    alert = _scan(tmp_path, monkeypatch, REAL_ERROR)
    assert alert is not None
    assert "Error in _tts_inference_task" in alert and "APIConnectionError: all TTSs failed" in alert
    assert "Снова на связи" not in alert and "lk.pii" not in alert and "room" not in alert


def test_self_healing_failures_stay_quiet(tmp_path, monkeypatch):
    assert _scan(tmp_path, monkeypatch, SELF_HEALING) is None


def test_edge_tts_skips_text_with_nothing_to_say():
    from livekit.agents import APIConnectOptions

    from custom_tts import TTS as EdgeTTS

    async def run() -> int:
        tts = EdgeTTS(voice="ru-RU-DmitryNeural")
        frames = 0
        async with tts.synthesize(" .", conn_options=APIConnectOptions(max_retry=0)) as stream:
            async for _ in stream:
                frames += 1
        return frames

    assert asyncio.run(run()) >= 1  # a beat of silence: no NoAudioReceived, no network call
