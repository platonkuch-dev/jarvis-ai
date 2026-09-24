"""Entrypoint for the voice agent worker.

Pipeline: Deepgram Nova-3 (STT) -> Claude Haiku (LLM) -> edge-tts (TTS),
gated by Silero VAD. Run with:

    python worker.py dev      # connect to LiveKit Playground / a dev room
    python worker.py start    # production worker mode

See README.md for setup and example voice commands.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    JobProcess,
    WorkerOptions,
    cli,
)
from livekit.plugins import anthropic, deepgram, elevenlabs, openai as openai_plugin, silero

import config
import hud_bridge
import prompts
from custom_tts import TTS as EdgeTTS
from sleep_wake import SleepWakeController
from tools import FUNCTION_TOOLS
from tools import runtime as tool_runtime
from tools.memory import load_memory

load_dotenv()

logger = logging.getLogger("jarvis-voice-agent")

# A global OS hotkey only makes sense for the desktop app (worker.py console);
# a `dev`/`start` job may be serving a remote browser with nobody at this
# keyboard, so the wake hotkey / auto-sleep are console-mode only.
IS_CONSOLE_MODE = "console" in sys.argv


class JarvisAgent(Agent):
    def __init__(self, instructions: str) -> None:
        super().__init__(instructions=instructions, tools=FUNCTION_TOOLS)

    async def on_enter(self) -> None:
        self.session.generate_reply(
            instructions="Поздоровайся коротко и представься как голосовой помощник Джарвис."
        )


def prewarm(proc: JobProcess) -> None:
    # Loading the Silero ONNX model is blocking; do it once per worker process
    # here instead of per-session in the entrypoint.
    proc.userdata["vad"] = silero.VAD.load()


def _persisted_voice_id() -> str | None:
    """A voice picked by voice command (tools/voice_control.py) persists here
    so it survives a restart instead of reverting to ELEVENLABS_VOICE_ID."""
    try:
        import json

        return json.loads(config.TTS_VOICE_STATE_FILE.read_text(encoding="utf-8")).get("voice_id")
    except Exception:
        return None


def _build_llm():
    if config.LLM_PROVIDER == "openai":
        from openai.types import Reasoning

        # use_websocket=True (the plugin's default) talks to the Responses
        # API directly over wss://api.openai.com/v1/responses -- lower
        # latency than HTTP for a live voice turn, matching why the
        # Anthropic path below also avoids anything that adds round trips.
        # api_key omitted for the same reason as the Anthropic call: passing
        # NOT_GIVEN (the default) lets the plugin fall back to the
        # OPENAI_API_KEY env var itself; an explicit None would not.
        return openai_plugin.responses.LLM(
            model=config.OPENAI_MODEL,
            reasoning=Reasoning(effort=config.OPENAI_REASONING_EFFORT),
        )
    # api_key omitted here on purpose: the plugin reads ANTHROPIC_API_KEY
    # from the environment itself, and passing an explicit None would
    # short-circuit that fallback.
    # _strict_tool_schema=False: Anthropic caps "strict" tool schemas at 20
    # tools, and this agent ships 40+. Our schemas are already simple
    # (no open-ended objects), so non-strict validation loses nothing here.
    # caching="ephemeral": the system prompt + all tool schemas measure
    # ~11.4k tokens (system ~1.8k, tools ~9.6k -- after_effects_control's
    # docstring alone is ~2.3k) and were being resent as fresh, full-price
    # input tokens on every single conversational turn with no caching at
    # all -- confirmed live by inspecting the exact JSON the anthropic
    # plugin sends (see to_fnc_ctx) and worker.py's LLM call, which never
    # set this. Ephemeral caching writes that fixed prefix once and reuses
    # it (~90% cheaper) on every turn within its 5-minute window, which
    # covers essentially every real back-and-forth in a live conversation.
    return anthropic.LLM(model=config.ANTHROPIC_MODEL, _strict_tool_schema=False, caching="ephemeral")


def _build_tts():
    if config.TTS_PROVIDER == "elevenlabs":
        # api_key passed explicitly: the plugin's own env fallback is
        # ELEVEN_API_KEY, not ELEVENLABS_API_KEY, so it wouldn't pick up
        # our .env value on its own.
        kwargs = {
            "model": config.ELEVENLABS_MODEL,
            "api_key": config.ELEVENLABS_API_KEY,
            # Lower stability + some style so delivery varies naturally
            # instead of sounding flat/robotic; see config.py for tuning.
            "voice_settings": elevenlabs.VoiceSettings(
                stability=config.ELEVENLABS_STABILITY,
                similarity_boost=config.ELEVENLABS_SIMILARITY_BOOST,
                style=config.ELEVENLABS_STYLE,
                use_speaker_boost=True,
            ),
        }
        voice_id = _persisted_voice_id() or config.ELEVENLABS_VOICE_ID
        if voice_id:
            kwargs["voice_id"] = voice_id
        return elevenlabs.TTS(**kwargs)
    return EdgeTTS(
        voice=config.TTS_VOICE, rate=config.TTS_RATE, volume=config.TTS_VOLUME, pitch=config.TTS_PITCH
    )


async def entrypoint(ctx: JobContext) -> None:
    ctx.log_context_fields = {"room": ctx.room.name}

    if config.SYSTEM == "Windows":
        try:
            from adobe_cep_bridge import installer as adobe_cep_installer

            logger.info(adobe_cep_installer.ensure_installed())
        except Exception:
            logger.warning("could not set up the Adobe CEP bridge", exc_info=True)

        try:
            from tools import app_launcher

            indexed = await asyncio.to_thread(app_launcher.build_app_index)
            logger.info("indexed %d Start Menu shortcuts for app launching", indexed)
        except Exception:
            logger.warning("could not build the Start Menu shortcut index", exc_info=True)

    session: AgentSession = AgentSession(
        stt=deepgram.STT(model=config.DEEPGRAM_MODEL, language=config.DEEPGRAM_LANGUAGE),
        llm=_build_llm(),
        tts=_build_tts(),
        vad=ctx.proc.userdata["vad"],
    )

    async def _log_usage() -> None:
        logger.info("session usage: %s", session.usage)

    ctx.add_shutdown_callback(_log_usage)

    hud_lines: list[dict[str, str]] = []
    sleep_wake = SleepWakeController(session, hud_lines) if IS_CONSOLE_MODE else None

    @session.on("user_input_transcribed")
    def _on_user_transcribed(ev) -> None:
        if sleep_wake is not None:
            sleep_wake.note_activity()
        if not ev.is_final and (sleep_wake is None or not sleep_wake.asleep):
            hud_bridge.write_state("listening", hud_lines)

    @session.on("conversation_item_added")
    def _on_item_added(ev) -> None:
        # NOTE: for the assistant this fires once the turn is fully done --
        # at or after the point its audio finishes playing, not when
        # generation starts -- so it must never be the thing that sets
        # "speaking" (see the playback_started/playback_finished handlers
        # below): doing so raced the playback_finished reset and lost,
        # leaving the bar stuck showing "speaking" forever after each reply.
        if sleep_wake is not None:
            sleep_wake.note_activity()
        role = getattr(ev.item, "role", None)
        text = getattr(ev.item, "text_content", None)
        if role is None or not text:
            return
        hud_lines.append({"role": role, "text": text})
        if role == "user":
            hud_bridge.write_state("thinking", hud_lines)

    memory = await load_memory()
    agent = JarvisAgent(prompts.build_instructions(memory))

    await ctx.connect()
    await session.start(agent=agent, room=ctx.room)
    hud_bridge.write_state("idle", hud_lines)

    # The actual start/end of the agent's own audio -- the correct signal
    # for "speaking", unlike conversation_item_added (see note above).
    if session.output.audio is not None:

        @session.output.audio.on("playback_started")
        def _on_playback_started(ev) -> None:
            if sleep_wake is not None:
                sleep_wake.note_activity()
            hud_bridge.write_state("speaking", hud_lines)

        @session.output.audio.on("playback_finished")
        def _on_playback_finished(ev) -> None:
            if sleep_wake is not None:
                sleep_wake.note_activity()
            hud_bridge.write_state("listening", hud_lines)

    # Lets background tasks (reminders, timers, scenario narration/progress)
    # speak and publish into this room without needing a RunContext of their own.
    tool_runtime.set_active_session(session)
    tool_runtime.set_active_room(ctx.room)
    tool_runtime.set_active_agent(agent)

    if sleep_wake is not None:
        sleep_wake.start()

        async def _stop_sleep_wake() -> None:
            sleep_wake.stop()

        ctx.add_shutdown_callback(_stop_sleep_wake)


if __name__ == "__main__":
    # agent_name: required so the SIP dispatch rule (phone number -> this
    # agent) can target it explicitly by name. `console` mode (the desktop
    # app's normal path via app.py) never goes through room dispatch at all,
    # so this has no effect there -- it only matters for `start`/`dev`,
    # which now need an explicit dispatch (matching this name) to receive a
    # room instead of joining every room automatically like before.
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm, agent_name="jarvis"))
