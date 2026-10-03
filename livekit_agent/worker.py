"""Entrypoint for the voice agent worker.

Pipeline: Deepgram Nova-3 (STT) -> Claude Haiku / Claude Code CLI / local Ollama model (LLM) -> TTS,
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

import screens  # noqa: F401  (per-monitor DPI awareness before anything imports pyautogui)
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    JobProcess,
    WorkerOptions,
    cli,
)
from livekit import rtc
from livekit.agents import llm, stt, tts
from livekit.plugins import anthropic, deepgram, elevenlabs, openai as openai_plugin, silero

import config
import fast_path
import hud_bridge
import journal
import memes
import notify
import pc_guard
import prompts
import usage
from custom_tts import TTS as EdgeTTS
from sleep_wake import SleepWakeController
from tools import FUNCTION_TOOLS
from tools import runtime as tool_runtime
from tools import scheduling, shell, tasks, triggers, web
from tools.memory import load_memory

load_dotenv()

logger = logging.getLogger("jarvis-voice-agent")

# A global OS hotkey only makes sense for the desktop app (worker.py console);
# a `dev`/`start` job may be serving a remote browser with nobody at this
# keyboard, so the wake hotkey / auto-sleep are console-mode only.
IS_CONSOLE_MODE = "console" in sys.argv

if IS_CONSOLE_MODE and config.HUD_FACE:
    # Feeds the HUD face's lips from the audio actually being played.
    import lipsync_bridge

    lipsync_bridge.install_console_tap()

# Strong refs for fire-and-forget tasks (asyncio keeps only weak ones).
_BACKGROUND_REFS: set[asyncio.Task] = set()


_RESTRICTED_CALLER_NOTE = """

[Сейчас тебе звонит по телефону человек, чей номер НЕ в списке доверенных. У тебя в этом
разговоре нет инструментов и нет доступа к компьютеру владельца: ничего не обещай сделать,
не раскрывай никаких сведений о владельце и его жизни. Можно просто вежливо поговорить или
предложить оставить сообщение — скажи, что передашь его владельцу.]
"""


class JarvisAgent(Agent):
    def __init__(self, instructions: str, *, restricted: bool = False) -> None:
        super().__init__(instructions=instructions, tools=[] if restricted else FUNCTION_TOOLS)
        # restricted: an untrusted phone caller -- no tools, and no local
        # fast path either (it would run tools without the LLM).
        self._restricted = restricted

    async def on_enter(self) -> None:
        if config.JARVIS_PERSONA == "roast" and not self._restricted:
            greeting = "Поздоровайся одной короткой фразой в своём характере, с лёгким подколом владельца."
        else:
            greeting = "Поздоровайся коротко и представься как голосовой помощник Джарвис."
        self.session.generate_reply(instructions=greeting)

    async def on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage) -> None:
        """Handles the most common one-shot commands locally (fast_path.py),
        skipping the LLM round trip entirely: no tokens, and the reply starts
        about a second sooner."""
        if self._restricted:
            if new_message.text_content:
                notify.notify_owner(f"📞 Звонящий не из списка сказал: {new_message.text_content[:500]}",
                                    kind="phone")
            return
        text = new_message.text_content or ""
        # A "да"/"нет" to a dangerous step pc_guard is holding back: record it
        # and tell the model, instead of letting the fast path near it.
        guard_note = pc_guard.note_user_reply(text)
        if guard_note is not None:
            new_message.content.append(guard_note)
            return
        from tools import computer_use

        intent = fast_path.parse(text, computer_use_running=computer_use._RUN_LOCK.locked())
        if intent is None:
            return
        handled, reply = await fast_path.execute(intent)
        if not handled:
            return  # let the LLM take it from here, exactly as before
        logger.info("fast path: %r -> %s", text, intent.tool or "local answer")
        # Keep the exchange in the LLM's history so follow-ups ("а теперь
        # громче") still make sense to it on the next, non-fast-path turn.
        chat_ctx = self.chat_ctx.copy()
        chat_ctx.add_message(role="user", content=text)
        await self.update_chat_ctx(chat_ctx)
        self.session.say(reply)
        raise llm.StopResponse()

    async def llm_node(self, chat_ctx, tools, model_settings):
        # A local model's context is 16k and the prompt + tool schemas take
        # ~13k of it; past that, Ollama silently cuts from the *front* -- the
        # system prompt goes first. Keep only the recent turns (the system
        # message is always kept, so the cached prompt prefix still hits).
        if config.LLM_PROVIDER == "ollama":
            chat_ctx = chat_ctx.copy().truncate(max_items=config.OLLAMA_MAX_HISTORY_ITEMS)
        async for chunk in Agent.default.llm_node(self, chat_ctx, tools, model_settings):
            yield chunk

    async def tts_node(self, text, model_settings):
        # [смех] / [мем: ...] tags in the reply become real audio clips,
        # spliced into the speech stream at the spot the model put them.
        async for frame in memes.tts_with_sfx(self, text, model_settings):
            yield frame

    async def transcription_node(self, text, model_settings):
        async for delta in memes.strip_transcription(text):
            yield delta


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


def _build_llm(instructions: str = "", *, mcp_server=None, restricted: bool = False):
    if config.LLM_PROVIDER == "claude_code":
        # The local Claude Code CLI, on the Claude.ai subscription it is
        # logged into (see claude_code_llm.py). Jarvis's tools reach it over
        # MCP; an untrusted phone caller gets no tools at all.
        from claude_code_llm import ClaudeCodeLLM

        return ClaudeCodeLLM(
            system_prompt=instructions,
            mcp_config=mcp_server.mcp_config() if mcp_server is not None else None,
            restricted=restricted,
            persist_session=IS_CONSOLE_MODE,
        )
    if config.LLM_PROVIDER == "ollama":
        # Local model via Ollama's OpenAI-compatible endpoint: free, no
        # network hop. parallel_tool_calls off -- small local models get
        # noticeably sloppier when asked to emit several calls at once.
        # reasoning_effort="none": Qwen3 otherwise "thinks" 150-300 hidden
        # tokens before every reply (~2-3 s of silence on a 5070; measured).
        # A "/no_think" prompt switch did NOT stop it on Ollama 0.33 -- only
        # this request field does.
        return openai_plugin.LLM.with_ollama(
            model=config.OLLAMA_MODEL,
            base_url=config.OLLAMA_BASE_URL,
            temperature=config.OLLAMA_TEMPERATURE,
            parallel_tool_calls=False,
            reasoning_effort=config.OLLAMA_REASONING_EFFORT,
        )
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


async def _pin_ollama_model() -> None:
    """Load the local model into VRAM now and keep it there (keep_alive=-1):
    otherwise Ollama unloads it after 5 idle minutes and the next question
    waits several seconds for a reload."""
    import aiohttp

    root = config.OLLAMA_BASE_URL.rstrip("/").removesuffix("/v1")
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(f"{root}/api/generate", json={"model": config.OLLAMA_MODEL, "keep_alive": -1},
                                 timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status != 200:
                    logger.warning("ollama could not load %s: %s", config.OLLAMA_MODEL, await resp.text())
                else:
                    logger.info("ollama model %s loaded and pinned", config.OLLAMA_MODEL)
    except Exception:
        logger.warning("ollama is not reachable at %s -- is it running?", root, exc_info=True)


def _build_stt(vad):
    """Deepgram, with OpenAI transcription as the backup ear when an OpenAI
    key is set: a Deepgram outage or an empty Deepgram balance used to leave
    Jarvis deaf ("failed to recognize speech" until restart). The backup
    costs nothing while Deepgram is healthy -- it is only called on failure."""
    primary = deepgram.STT(model=config.DEEPGRAM_MODEL, language=config.DEEPGRAM_LANGUAGE)
    if not config.OPENAI_API_KEY:
        return primary
    backup = openai_plugin.STT(model="gpt-4o-mini-transcribe", language=config.DEEPGRAM_LANGUAGE,
                               api_key=config.OPENAI_API_KEY, use_realtime=False)
    return stt.FallbackAdapter([primary, backup], vad=vad)


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
        # Edge as the backup voice: a rejected key, an empty ElevenLabs
        # balance or a deleted voice_id used to leave Jarvis mute (only
        # "failed to synthesize speech" in the log); now he keeps talking
        # in the free voice and switches back once ElevenLabs recovers.
        edge = EdgeTTS(voice=config.TTS_VOICE, rate=config.TTS_RATE, volume=config.TTS_VOLUME, pitch=config.TTS_PITCH)
        return tts.FallbackAdapter([elevenlabs.TTS(**kwargs), edge], max_retry_per_tts=1)
    return EdgeTTS(
        voice=config.TTS_VOICE, rate=config.TTS_RATE, volume=config.TTS_VOLUME, pitch=config.TTS_PITCH
    )


async def entrypoint(ctx: JobContext) -> None:
    ctx.log_context_fields = {"room": ctx.room.name}

    if config.LLM_PROVIDER == "ollama":
        _BACKGROUND_REFS.add(asyncio.create_task(_pin_ollama_model(), name="ollama-pin"))

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

    # Phone calls: only numbers in PHONE_ALLOWED_NUMBERS get tools. The
    # caller has to be identified before the agent (and its tool list) exists.
    restricted = False
    if not IS_CONSOLE_MODE:
        await ctx.connect()
        participant = await ctx.wait_for_participant()
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            caller = "".join(ch for ch in participant.attributes.get("sip.phoneNumber", "") if ch.isdigit())
            restricted = not caller or caller not in config.PHONE_ALLOWED_NUMBERS
            logger.info("phone call from %s -- %s", caller or "unknown number",
                        "conversation only" if restricted else "trusted, full tools")

    if restricted:
        agent = JarvisAgent(prompts.SYSTEM_PROMPT + _RESTRICTED_CALLER_NOTE, restricted=True)
    else:
        owner_memory = await load_memory()
        agent = JarvisAgent(prompts.build_instructions(owner_memory))
    # Only the owner's own desktop session goes into the shared journal
    # (journal.py) -- phone callers are not the owner.
    journal_owner = None
    if IS_CONSOLE_MODE and not restricted:
        journal_owner = ((owner_memory.get("identity") or {}).get("name") or {}).get("value") or "Владелец"

    mcp_server = None
    session_kwargs = {}
    if config.LLM_PROVIDER == "claude_code":
        if not restricted:
            from jarvis_mcp import JarvisMCPServer

            # run_powershell / read_webpage are for the other brains: claude has
            # its own PowerShell (judged by the same pc_guard through
            # permission_gate) and WebFetch.
            own_equivalent = {shell.run_powershell, web.read_webpage}
            mcp_server = JarvisMCPServer([t for t in FUNCTION_TOOLS if t not in own_equivalent],
                                         extra=[pc_guard.gate_tool()])
            await mcp_server.start()
            ctx.add_shutdown_callback(mcp_server.aclose)
        # Preemptive generation starts a reply on an interim transcript and
        # throws it away if the final one differs -- fine for a stateless
        # API, but here every start is a real message in claude's history.
        session_kwargs["turn_handling"] = {"preemptive_generation": {"enabled": False}}

    session: AgentSession = AgentSession(
        stt=_build_stt(ctx.proc.userdata["vad"]),
        llm=_build_llm(agent.instructions, mcp_server=mcp_server, restricted=restricted),
        tts=_build_tts(),
        vad=ctx.proc.userdata["vad"],
        **session_kwargs,
    )

    async def _log_usage() -> None:
        logger.info("session usage: %s", session.usage)

    ctx.add_shutdown_callback(_log_usage)

    @session.on("metrics_collected")
    def _on_metrics(ev) -> None:
        m = ev.metrics
        if getattr(m, "type", None) == "llm_metrics" and config.LLM_PROVIDER not in ("ollama", "claude_code"):
            # prompt_tokens already includes the cached part; split it back out
            # so cache reads/writes are priced at their real (lower/higher) rates.
            cached, written = m.prompt_cached_tokens or 0, getattr(m, "cache_creation_tokens", 0) or 0
            model = config.OPENAI_MODEL if config.LLM_PROVIDER == "openai" else config.ANTHROPIC_MODEL
            usage.record(model, max(0, m.prompt_tokens - cached - written), m.completion_tokens,
                         cached, written, source="voice")

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
        text = memes.strip_tags(text)
        if not text:
            return
        hud_lines.append({"role": role, "text": text})
        if journal_owner is not None and role in ("user", "assistant"):
            if role == "user":
                journal.append("voice", "", journal_owner, "owner", text)
            else:
                journal.append("voice", "", "Джарвис", "jarvis", text)
        if role == "user" and (sleep_wake is None or not sleep_wake.asleep):
            hud_bridge.write_state("thinking", hud_lines)

    if IS_CONSOLE_MODE:
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
            hud_bridge.write_state("sleeping" if sleep_wake is not None and sleep_wake.asleep else "speaking", hud_lines)

        @session.output.audio.on("playback_finished")
        def _on_playback_finished(ev) -> None:
            if sleep_wake is not None:
                sleep_wake.note_activity()
            # an F10 interrupt ends playback after the HUD already went to sleep: keep it asleep
            hud_bridge.write_state("sleeping" if sleep_wake is not None and sleep_wake.asleep else "listening", hud_lines)

    # Lets background tasks (reminders, timers, scenario narration/progress)
    # speak and publish into this room without needing a RunContext of their own.
    tool_runtime.set_active_session(session)
    tool_runtime.set_active_room(ctx.room)
    tool_runtime.set_active_agent(agent)

    if sleep_wake is not None:
        sleep_wake.start()
        tool_runtime.set_presence_probe(lambda: not sleep_wake.asleep)

        async def _stop_sleep_wake() -> None:
            sleep_wake.stop()

        ctx.add_shutdown_callback(_stop_sleep_wake)

    # The autonomy loops run only in the desktop worker (console mode): the
    # phone worker is a second process on the same machine, and running them
    # there too would fire every reminder/trigger/task twice.
    if IS_CONSOLE_MODE:
        background = [
            asyncio.create_task(scheduling.reminder_loop(), name="reminders"),
            asyncio.create_task(tasks.task_runner_loop(), name="tasks"),
            asyncio.create_task(triggers.trigger_loop(), name="triggers"),
        ]

        async def _stop_background() -> None:
            for task in background:
                task.cancel()

        ctx.add_shutdown_callback(_stop_background)


if __name__ == "__main__":
    # agent_name: required so the SIP dispatch rule (phone number -> this
    # agent) can target it explicitly by name. `console` mode (the desktop
    # app's normal path via app.py) never goes through room dispatch at all,
    # so this has no effect there -- it only matters for `start`/`dev`,
    # which now need an explicit dispatch (matching this name) to receive a
    # room instead of joining every room automatically like before.
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm, agent_name="jarvis"))
