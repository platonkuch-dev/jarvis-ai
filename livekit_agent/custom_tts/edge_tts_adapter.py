"""Custom LiveKit TTS plugin backed by Microsoft Edge's TTS service via `edge-tts`.

Implements the plugin contract exactly as LiveKit's own non-streaming plugins
do (see livekit-plugins-openai's `tts.py` for the reference this mirrors):
`TTS.synthesize()` returns a `ChunkedStream`, whose `_run()` initializes an
`AudioEmitter` with the real mime type and pushes raw bytes into it -- the
emitter's own decoder handles turning mp3 into PCM frames, so we never touch
audio decoding ourselves.

edge-tts streams audio as mp3 at 24kHz mono, which is why SAMPLE_RATE/
NUM_CHANNELS below match the OpenAI TTS plugin's own mp3 defaults.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace

import edge_tts
from livekit.agents import APIConnectionError, APIConnectOptions, tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

SAMPLE_RATE = 24000
NUM_CHANNELS = 1

DEFAULT_VOICE = "ru-RU-DmitryNeural"


@dataclass
class _TTSOptions:
    voice: str
    rate: str
    volume: str
    pitch: str


class TTS(tts.TTS):
    """Non-streaming TTS: edge-tts synthesizes one utterance at a time."""

    def __init__(
        self,
        *,
        voice: str = DEFAULT_VOICE,
        rate: str = "+0%",
        volume: str = "+0%",
        pitch: str = "+0Hz",
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
        )
        self._opts = _TTSOptions(voice=voice, rate=rate, volume=volume, pitch=pitch)

    @property
    def model(self) -> str:
        return "edge-tts"

    @property
    def provider(self) -> str:
        return "microsoft-edge-tts"

    def update_options(
        self,
        *,
        voice: str | None = None,
        rate: str | None = None,
        volume: str | None = None,
        pitch: str | None = None,
    ) -> None:
        if voice is not None:
            self._opts.voice = voice
        if rate is not None:
            self._opts.rate = rate
        if volume is not None:
            self._opts.volume = volume
        if pitch is not None:
            self._opts.pitch = pitch

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.ChunkedStream:
        return ChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    async def aclose(self) -> None:
        return None


class ChunkedStream(tts.ChunkedStream):
    def __init__(self, *, tts: TTS, input_text: str, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._opts = replace(tts._opts)

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        # A segment with nothing to pronounce (" .", "—", an emoji left over
        # after the sentence splitter) makes Edge answer NoAudioReceived; the
        # FallbackAdapter then marked Edge dead -- and kept failing its
        # recovery on the same " ." -- so with ElevenLabs down Jarvis went
        # silent. Nothing to say is not an error: a 20 ms beat of silence
        # (livekit requires at least one frame for non-empty text).
        if not any(ch.isalnum() for ch in self.input_text):
            output_emitter.initialize(request_id=uuid.uuid4().hex, sample_rate=SAMPLE_RATE,
                                      num_channels=NUM_CHANNELS, mime_type="audio/pcm")
            output_emitter.push(bytes(SAMPLE_RATE // 50 * 2 * NUM_CHANNELS))
            output_emitter.flush()
            return
        output_emitter.initialize(
            request_id=uuid.uuid4().hex,
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
            mime_type="audio/mpeg",
        )
        try:
            communicate = edge_tts.Communicate(
                self.input_text,
                voice=self._opts.voice,
                rate=self._opts.rate,
                volume=self._opts.volume,
                pitch=self._opts.pitch,
            )
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    output_emitter.push(chunk["data"])
        except Exception as exc:
            raise APIConnectionError() from exc
        finally:
            output_emitter.flush()
