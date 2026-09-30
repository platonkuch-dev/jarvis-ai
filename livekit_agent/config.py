"""Central configuration: paths, whitelists, and env-driven settings.

Nothing here executes arbitrary user input. `SCRIPT_WHITELIST` and
`SAFE_SYSTEM_ACTIONS` are the only two places that gate real side effects on
the host machine, on purpose -- keep every new script or system action
declared here instead of letting a tool improvise a path or a shell command.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
NOTES_DIR = DATA_DIR / "notes"
SCREENSHOTS_DIR = DATA_DIR / "screenshots"
SCRIPTS_LOG_DIR = LOGS_DIR / "scripts"
# Webcam frames are far more sensitive than a desktop screenshot (a person's
# face/room, not a window). tools/camera.py deletes each capture from here
# the moment it's been sent to Claude and the reply is back -- the directory
# exists only because QImageCapture needs a real path to save to, never as
# a photo library.
CAMERA_CAPTURES_DIR = DATA_DIR / "camera_captures"

for _d in (DATA_DIR, LOGS_DIR, NOTES_DIR, SCREENSHOTS_DIR, SCRIPTS_LOG_DIR, CAMERA_CAPTURES_DIR):
    _d.mkdir(parents=True, exist_ok=True)

ENV_FILE = BASE_DIR / ".env"
SCENARIOS_FILE = DATA_DIR / "scenarios.json"
TODOS_FILE = DATA_DIR / "todos.json"
REMINDERS_FILE = DATA_DIR / "reminders.json"
EVENTS_FILE = DATA_DIR / "events.json"
TOOL_LOG_FILE = LOGS_DIR / "tool_calls.log"

# --- Long-term memory + pattern learning ---
# Ported from the original Jarvis project's memory/memory_manager.py and
# memory/pattern_learning.py (same schema, same limits) rather than a new
# design -- see tools/memory.py and tools/pattern_learning.py.
MEMORY_FILE = DATA_DIR / "memory.json"
MEMORY_MAX_CHARS = int(os.environ.get("MEMORY_MAX_CHARS", "2200"))
MEMORY_MAX_VALUE_LEN = int(os.environ.get("MEMORY_MAX_VALUE_LEN", "380"))
# How many recent tool calls trigger a background pattern-learning pass, and
# how many trailing log lines that pass looks at (tools/_logging.py already
# keeps the full history in TOOL_LOG_FILE; this only bounds one analysis read).
# Doubled from the original 20 -- this fires a real (small) LLM call each
# time purely for a "nice to have" behavioral-pattern summary, not anything
# the assistant strictly needs to function.
PATTERN_ANALYZE_EVERY = int(os.environ.get("PATTERN_ANALYZE_EVERY", "40"))
PATTERN_MAX_LOG_LINES = int(os.environ.get("PATTERN_MAX_LOG_LINES", "500"))

SYSTEM = platform.system()  # "Windows", "Darwin", "Linux"

# ---------------------------------------------------------------------------
# LiveKit / provider credentials (read from environment / .env)
# ---------------------------------------------------------------------------
LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "")

# --- Telegram (tools/telegram_dm.py) ---
# A real Telegram user account dedicated to Jarvis (not a Bot API bot) --
# bots can't message someone who hasn't messaged them first, a real account
# can. api_id/api_hash come from my.telegram.org; the session file (created
# by running telegram_login.py once, interactively) holds the actual login,
# these two only identify the client application.
TELEGRAM_API_ID = int(os.environ.get("TELEGRAM_API_ID", "0") or "0")
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
TELEGRAM_PHONE = os.environ.get("TELEGRAM_PHONE", "")
TELEGRAM_SESSION_PATH = str(DATA_DIR / "jarvis_telegram")

# --- Telegram chat bridge (telegram_bridge.py) ---
# Own copy of the session (bootstrapped from the one above) so this
# long-lived listener connection and tools/telegram_dm.py's occasional
# send-only connection never contend for the same local SQLite session file.
TELEGRAM_BRIDGE_SESSION_PATH = str(DATA_DIR / "jarvis_telegram_bridge")
# Whoever sends the one-time pairing code from the control panel claims this
# file (telegram_owner.py) and is the only one who gets real tool access;
# everyone else gets plain conversation with no tools and no injected
# personal memory. "Сменить владельца" in the panel resets it.
TELEGRAM_OWNER_FILE = DATA_DIR / "telegram_owner.json"
TELEGRAM_BRIDGE_MAX_STEPS = int(os.environ.get("TELEGRAM_BRIDGE_MAX_STEPS", "8"))
# Turns, not raw messages -- one turn can be several messages (a tool_use /
# tool_result round-trip plus the final reply), so this bounds conversation
# depth without ever risking a trim landing mid-round-trip.
TELEGRAM_BRIDGE_MAX_HISTORY = 8
# Persisted so a restart (this project restarts a lot -- crashes, updates,
# manual relaunches) doesn't wipe every open conversation and force it to
# start cold with people mid-chat.
TELEGRAM_HISTORY_FILE = DATA_DIR / "telegram_history.json"
# Per-sender profile (name, first/last seen, message count, and any notes
# Jarvis chose to save via remember_contact_note) -- separate from
# tools/memory.py's facts, which are about the account OWNER only. Facts a
# random Telegram stranger causes Jarvis to note down must never bleed into
# that owner memory, so this is its own file with its own tool.
TELEGRAM_CONTACTS_FILE = DATA_DIR / "telegram_contacts.json"
TELEGRAM_CONTACT_MAX_NOTES = 20
# Facts the owner tells Jarvis about someone who hasn't messaged the bridge
# yet (so there's no contact record to attach the note to), keyed by the
# name as given -- merged in the moment a matching name first messages.
TELEGRAM_PENDING_NOTES_FILE = DATA_DIR / "telegram_pending_notes.json"
# Strangers get plain Haiku chat; this caps how many of their messages per
# day get an (API-billed) answer, so spam can't run up the bill.
TELEGRAM_STRANGER_MAX_PER_DAY = int(os.environ.get("TELEGRAM_STRANGER_MAX_PER_DAY", "20"))

# ---------------------------------------------------------------------------
# Autonomy: background tasks, triggers, approvals, spending limit
# ---------------------------------------------------------------------------
TASKS_FILE = DATA_DIR / "tasks.json"
TRIGGERS_FILE = DATA_DIR / "triggers.json"
USAGE_FILE = DATA_DIR / "usage.json"
# How long a background task waits for the owner's "да"/"нет" on Telegram
# before treating silence as "no" (approvals.py).
APPROVAL_TIMEOUT_S = float(os.environ.get("APPROVAL_TIMEOUT_S", "600"))
# Hard daily ceiling on paid LLM spend, in USD, counted from real token usage
# (usage.py). Past it, background tasks and use_computer refuse to start and
# the owner gets one Telegram notice; plain conversation keeps working.
# 0 disables the limit.
DAILY_BUDGET_USD = float(os.environ.get("DAILY_BUDGET_USD", "3"))
TASK_MAX_STEPS = int(os.environ.get("TASK_MAX_STEPS", "12"))
# Child-process logs (app.py) and tool_calls.log roll over past this size.
LOG_MAX_BYTES = int(os.environ.get("LOG_MAX_BYTES", str(5 * 1024 * 1024)))
# Phone callers (LiveKit SIP) whose number is in this comma-separated list get
# the full tool set; everyone else gets conversation only. Empty = nobody:
# a phone number is public, a caller ID is the only thing tying a call to you.
PHONE_ALLOWED_NUMBERS = [
    "".join(ch for ch in n if ch.isdigit())
    for n in os.environ.get("PHONE_ALLOWED_NUMBERS", "").split(",") if n.strip()
]

# --- Proactive system monitor (proactive_monitor.py) ---
# Own session copy again, same reason as the bridge -- a persistent-ish
# connection that must never contend with the other two for one session
# file. Pure Python + psutil checks, no Claude calls at all -- this is a
# free background feature, not a token-spending one.
TELEGRAM_MONITOR_SESSION_PATH = str(DATA_DIR / "jarvis_telegram_monitor")
MONITOR_CHECK_INTERVAL_S = float(os.environ.get("MONITOR_CHECK_INTERVAL_S", "300"))
MONITOR_DISK_FREE_PCT_THRESHOLD = float(os.environ.get("MONITOR_DISK_FREE_PCT_THRESHOLD", "10"))
MONITOR_MEMORY_PCT_THRESHOLD = float(os.environ.get("MONITOR_MEMORY_PCT_THRESHOLD", "90"))
MONITOR_LOG_ERROR_WINDOW = 50  # how many recent app.log lines to scan for ERROR

DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")
DEEPGRAM_MODEL = os.environ.get("DEEPGRAM_MODEL", "nova-3")
DEEPGRAM_LANGUAGE = os.environ.get("DEEPGRAM_LANGUAGE", "ru")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# claude-haiku-4-5-20251001 is the current Claude Haiku snapshot; override via
# env if a newer Haiku snapshot should be pinned instead.
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
# Model for background tasks (tasks.py). Haiku: tasks mostly chain existing
# tools, and anything visual goes through use_computer's own model anyway.
TASK_MODEL = os.environ.get("TASK_MODEL", ANTHROPIC_MODEL)

# --- Main conversation model provider (worker.py) ---
# "anthropic" (default, Claude) or "openai" (GPT-6 Sol/Luna, via the
# Responses API -- see livekit.plugins.openai.responses.LLM). Switching
# providers here only swaps which model answers the conversation and calls
# tools; every tool in tools/ is unchanged either way, since livekit-agents'
# function-tool layer is already provider-agnostic.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
# gpt-6-luna: OpenAI's fast/cheap GPT-6 tier, the closest match to Haiku's
# role here (snappy voice-turn latency matters more than deep reasoning for
# ordinary conversation). gpt-6-sol or gpt-6-astra can be set instead for
# more reasoning depth at the cost of turn latency.
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-6-luna")
# GPT-6 Sol/Luna support "none" (GPT-6 Astra does not); "low" keeps some
# reasoning for tool-use quality without adding much latency to a voice turn.
OPENAI_REASONING_EFFORT = os.environ.get("OPENAI_REASONING_EFFORT", "low")

# LLM_PROVIDER=ollama: the conversation runs on a local model, free and with
# no network round trip. The model needs a context window that fits the
# system prompt + ~40 tool schemas (~12k tokens) -- Ollama's default 4k
# silently truncates it, so scripts/setup_local_llm.bat builds a "jarvis"
# variant of the model with num_ctx raised (see that script).
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "jarvis-qwen3")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_TEMPERATURE = float(os.environ.get("OLLAMA_TEMPERATURE", "0.7"))
# "none" = answer straight away (fast voice turns); "low"/"medium" = think first.
OLLAMA_REASONING_EFFORT = os.environ.get("OLLAMA_REASONING_EFFORT", "none")
# Conversation items (messages + tool calls/results) sent to the local model.
OLLAMA_MAX_HISTORY_ITEMS = int(os.environ.get("OLLAMA_MAX_HISTORY_ITEMS", "24"))

# LLM_PROVIDER=claude_code: the conversation runs inside one long-lived local
# `claude` CLI process (claude_code_llm.py), billed against the Claude.ai
# subscription `claude` is logged into -- no ANTHROPIC_API_KEY involved. It
# gets Claude Code's own tools (PowerShell, files, web) plus every Jarvis
# tool, served to it over a local MCP endpoint (jarvis_mcp.py).
# Model alias or full id: "haiku" (fastest voice turns), "sonnet", "opus".
CLAUDE_CODE_MODEL = os.environ.get("CLAUDE_CODE_MODEL", "sonnet")
# Extended thinking adds seconds of silence before every spoken reply
# (measured ~3 s on haiku), so it is off unless asked for.
CLAUDE_CODE_THINKING = os.environ.get("CLAUDE_CODE_THINKING", "0").strip().lower() in ("1", "true", "yes", "on")
# Continue the same Claude Code conversation after a restart (--resume).
CLAUDE_CODE_RESUME = os.environ.get("CLAUDE_CODE_RESUME", "1").strip().lower() in ("1", "true", "yes", "on")
CLAUDE_CODE_SESSION_FILE = DATA_DIR / "claude_code_session.json"
# Where the `claude` process runs; its CLAUDE.md there is read every session.
CLAUDE_CODE_WORKDIR = Path(os.environ.get("CLAUDE_CODE_WORKDIR", str(DATA_DIR / "claude_workspace")))
# Full access to the PC with voice confirmation for dangerous steps:
# CLAUDE_CODE_ALLOWED_TOOLS run without asking; every other call (PowerShell,
# Bash, Write, Edit, ...) that Claude Code doesn't already see as read-only
# goes to pc_guard.py via --permission-prompt-tool -- harmless ones run,
# dangerous ones wait for the user's spoken "да", a few are never done.
# "bypassPermissions" skips all of that -- only if you really want that.
CLAUDE_CODE_PERMISSION_MODE = os.environ.get("CLAUDE_CODE_PERMISSION_MODE", "default")
CLAUDE_CODE_ALLOWED_TOOLS = os.environ.get(
    "CLAUDE_CODE_ALLOWED_TOOLS",
    "mcp__jarvis Skill Read Glob Grep WebSearch WebFetch TodoWrite",
).split()
CLAUDE_CODE_DISALLOWED_TOOLS = os.environ.get("CLAUDE_CODE_DISALLOWED_TOOLS", "").split()
# Every fixed drive is a working directory for claude, not just the home folder.
CLAUDE_CODE_FULL_DISK_ACCESS = os.environ.get("CLAUDE_CODE_FULL_DISK_ACCESS", "1").strip().lower() in (
    "1", "true", "yes", "on")
# Skills shipped with Jarvis (claude_skills/), copied into the workdir's
# .claude/skills on start so the brain can load them.
CLAUDE_SKILLS_SRC = BASE_DIR / "claude_skills"
# Longest a single Jarvis tool call (use_computer, browser_task) may run
# before Claude Code gives up on it.
CLAUDE_CODE_MCP_TOOL_TIMEOUT_S = int(os.environ.get("CLAUDE_CODE_MCP_TOOL_TIMEOUT_S", "1800"))
# With LLM_PROVIDER=claude_code *everything* runs on the subscription: the
# screen agent, the browser agent, background tasks, the Telegram chat,
# camera / vision / screen-watch looks and memory upkeep all go through
# `claude -p` sub-agents (cc_agent.py) instead of the Anthropic API.
SUBSCRIPTION_MODE = LLM_PROVIDER == "claude_code"
# creationflags for console child processes (claude.exe): no window pops up.
NO_WINDOW = 0x08000000 if platform.system() == "Windows" else 0  # CREATE_NO_WINDOW
# Sub-agents that need judgement (screen, tasks, camera, finding UI elements).
CLAUDE_CODE_AGENT_MODEL = os.environ.get("CLAUDE_CODE_AGENT_MODEL", "sonnet")
# Quick/cheap ones (browser steps, screen watching, memory upkeep, the quick
# tier of the screen agent).
CLAUDE_CODE_FAST_MODEL = os.environ.get("CLAUDE_CODE_FAST_MODEL", "haiku")

# --- Personality ---
# "roast": banter, mild-to-strong swearing, laughs and memes where they fit.
# "classic": the original polite concise assistant. Untrusted phone callers
# always get the plain prompt regardless of this setting.
JARVIS_PERSONA = os.environ.get("JARVIS_PERSONA", "classic").strip().lower()

# --- Memes / sound effects spliced into speech (memes.py) ---
# Drop .mp3/.wav/.ogg files into data/memes/; the file name (without the
# extension) is the meme's name the LLM sees. Put laugh clips into
# data/memes/смех/ -- one is picked at random for every [смех] tag.
MEMES_DIR = DATA_DIR / "memes"
MEMES_DIR.mkdir(parents=True, exist_ok=True)
MEME_MAX_SECONDS = float(os.environ.get("MEME_MAX_SECONDS", "8"))
MEME_VOLUME = float(os.environ.get("MEME_VOLUME", "0.8"))

# --- "Heavy" computer-use agent (tools/computer_use.py) ---
# Haiku (ANTHROPIC_MODEL above) handles ordinary conversation and calls this
# only for multi-step visual tasks inside an application window (editing in
# Adobe apps, navigating a UI with no clean typed tool for it). Uses a
# separate, more capable model + Anthropic's computer-use toolset since it
# has to look at screenshots and plan a sequence of clicks/keystrokes, not
# just answer in words. Verified live against this account/SDK version
# (anthropic 0.125.0): "computer_toolset_20260801" needs no beta header and
# no declared display size -- each action (left_click/type/key/scroll/...)
# is its own named tool, and coordinates are read off the actual screenshot
# resolution. If this ever 400s after an SDK/API upgrade, check Anthropic's
# current computer-use docs -- the tool type name moves as the feature evolves.
COMPUTER_USE_MODEL = os.environ.get("COMPUTER_USE_MODEL", "claude-sonnet-5")

# "anthropic" (default) or "openai" -- switches tools/computer_use.py between
# Anthropic's computer_toolset (above) and GPT-6 Sol/Luna's native "computer"
# tool on the Responses API (GA, migrated from the old "computer_use_preview").
# Verified live (openai 2.54.0): tool def {"type": "computer"}, actions
# batched per computer_call (click/double_click/drag/move/scroll/keypress/
# type/wait/screenshot), state kept server-side via previous_response_id
# rather than a resent message history.
COMPUTER_USE_PROVIDER = os.environ.get("COMPUTER_USE_PROVIDER", "anthropic")
# gpt-6-sol: deep-reasoning tier, the closest match to Sonnet's role here.
OPENAI_COMPUTER_USE_MODEL = os.environ.get("OPENAI_COMPUTER_USE_MODEL", "gpt-6-sol")
OPENAI_COMPUTER_USE_REASONING_EFFORT = os.environ.get("OPENAI_COMPUTER_USE_REASONING_EFFORT", "medium")

# tools/camera.py + hud_bar.py's CameraPanel: how long the tool waits for
# each stage of the HUD-process round trip before giving up and speaking an
# error instead of hanging the conversation turn forever.
CAMERA_MODEL = os.environ.get("CAMERA_MODEL", "claude-sonnet-5")
CAMERA_OPEN_TIMEOUT_S = float(os.environ.get("CAMERA_OPEN_TIMEOUT_S", "5"))
CAMERA_CAPTURE_TIMEOUT_S = float(os.environ.get("CAMERA_CAPTURE_TIMEOUT_S", "6"))
# Give the sensor a beat to auto-expose/focus after the panel opens, so the
# first frame isn't a dark/blurry one grabbed mid-init.
CAMERA_WARMUP_S = float(os.environ.get("CAMERA_WARMUP_S", "0.7"))
COMPUTER_USE_TOOL_TYPE = os.environ.get("COMPUTER_USE_TOOL_TYPE", "computer_toolset_20260801")
# Ceilings per invocation. MAX_STEPS counts physical actions (screenshots and
# waits included). It used to be 12, which is what cut real runs short in
# logs/computer_use.log ("не удалось завершить задачу за 15 шагов"): every
# click is usually followed by a screenshot action, so 12 is only ~6 real
# steps. Raised 30->50 (and MAX_SECONDS 600->900) so longer in-app work
# (multi-step Premiere/Photoshop edits, multi-page registrations) doesn't
# hit an artificial ceiling instead of actually finishing -- safe to widen
# now that the stuck-action detector (see tools/computer_use.py's
# _STUCK_WARN/_STUCK_ABORT) independently catches a run going nowhere well
# before either cap would.
COMPUTER_USE_MAX_STEPS = int(os.environ.get("COMPUTER_USE_MAX_STEPS", "50"))
COMPUTER_USE_MAX_SECONDS = int(os.environ.get("COMPUTER_USE_MAX_SECONDS", "900"))
# Thinking depth. Anthropic's guidance for computer use is "high"; "low" was
# tried before and missed targets. Cheaper: "medium". Most accurate: "xhigh".
COMPUTER_USE_EFFORT = os.environ.get("COMPUTER_USE_EFFORT", "high")
# Fast tier for short tasks in one window (use_computer(simple=True), and
# quick_ui's fallback): lower thinking effort / the quick GPT-6 model, fewer
# steps. A fast run that gets stuck or hits its step cap is retried once on
# the full tier automatically. COMPUTER_USE_FAST_ENABLED=0 turns it off.
COMPUTER_USE_FAST_ENABLED = os.environ.get("COMPUTER_USE_FAST_ENABLED", "1") not in ("0", "false", "False", "")
COMPUTER_USE_FAST_EFFORT = os.environ.get("COMPUTER_USE_FAST_EFFORT", "low")
COMPUTER_USE_FAST_MAX_STEPS = int(os.environ.get("COMPUTER_USE_FAST_MAX_STEPS", "15"))
OPENAI_COMPUTER_USE_FAST_MODEL = os.environ.get("OPENAI_COMPUTER_USE_FAST_MODEL", "gpt-6-luna")
OPENAI_COMPUTER_USE_FAST_REASONING_EFFORT = os.environ.get("OPENAI_COMPUTER_USE_FAST_REASONING_EFFORT", "low")
# Longest side of the screenshots sent to the model. 1920 (~1080p) is the
# recommended accuracy/cost balance; 1280 made small UI text hard to read on a
# 1440p monitor. Sent as JPEG, and only the newest 3 are kept in the history.
COMPUTER_USE_MAX_IMAGE_DIM = int(os.environ.get("COMPUTER_USE_MAX_IMAGE_DIM", "1920"))
COMPUTER_USE_LOG_FILE = LOGS_DIR / "computer_use.log"

# --- Internet: web_search / read_webpage (tools/info.py, tools/web.py) ---
# DuckDuckGo region: "ru-ru" = Russian results first; "wt-wt" = no region.
WEB_SEARCH_REGION = os.environ.get("WEB_SEARCH_REGION", "ru-ru")
# Longest page text handed to the model by read_webpage.
WEB_PAGE_MAX_CHARS = int(os.environ.get("WEB_PAGE_MAX_CHARS", "8000"))

# --- Jarvis's own browser (tools/browser.py) ---
# A visible Chrome driven through the DOM (Playwright), not screenshots.
# Own profile, so logins made there persist and the everyday profile is
# never touched. "chrome" = the installed Google Chrome, "msedge" = Edge.
BROWSER_PROFILE_DIR = DATA_DIR / "browser_profile"
BROWSER_CHANNEL = os.environ.get("BROWSER_CHANNEL", "chrome")
# Text-only steps (element list in, action out) -- Haiku is plenty and fast.
BROWSER_MODEL = os.environ.get("BROWSER_MODEL", ANTHROPIC_MODEL)
BROWSER_MAX_STEPS = int(os.environ.get("BROWSER_MAX_STEPS", "30"))
BROWSER_LOG_FILE = LOGS_DIR / "browser.log"

# tools/screen_watch.py: optional, off-by-default background loop that
# watches the screen and only ever speaks up -- it never drives the mouse or
# keyboard itself (that's still use_computer, called explicitly). Cheap
# Haiku vision model, since this is a "notable or not" classification, not
# action planning. INTERVAL is how often it takes a local screenshot to
# check for change (matches the "раз в две секунды" the feature was asked
# for); a real API call only happens when the screenshot actually changed
# by more than DIFF_THRESHOLD, and never more often than MIN_LLM_GAP_S, so a
# static or a constantly-changing screen (video, scrolling) can't turn into
# nonstop paid calls.
SCREEN_WATCH_MODEL = os.environ.get("SCREEN_WATCH_MODEL", ANTHROPIC_MODEL)
SCREEN_WATCH_INTERVAL_S = float(os.environ.get("SCREEN_WATCH_INTERVAL_S", "2.0"))
SCREEN_WATCH_MIN_LLM_GAP_S = float(os.environ.get("SCREEN_WATCH_MIN_LLM_GAP_S", "6.0"))
# Fraction of a coarse 64x36 grayscale thumbnail that must differ from the
# previous tick before a real API call is even considered. Measured against
# a simulated 1920x1080 screen: a corner toast notification (~360x130px)
# diffs at ~0.026, a centered error dialog at ~0.08, a single changed line
# of text at ~0.012 -- 0.01 catches all of those. It will also catch normal
# typing/scrolling, but that only costs a cheap classification call (capped
# by MIN_LLM_GAP_S above): the classifier's own NOTHING-biased prompt is
# what filters routine activity from something worth speaking up about, not
# this local diff -- its only job is to skip calls on an unchanged screen.
SCREEN_WATCH_DIFF_THRESHOLD = float(os.environ.get("SCREEN_WATCH_DIFF_THRESHOLD", "0.01"))
SCREEN_WATCH_MAX_IMAGE_DIM = int(os.environ.get("SCREEN_WATCH_MAX_IMAGE_DIM", "1024"))
SCREEN_WATCH_LOG_FILE = LOGS_DIR / "screen_watch.log"

# TTS_PROVIDER selects the voice backend: "edge" (free, custom_tts/ adapter)
# or "elevenlabs" (paid, needs ELEVENLABS_API_KEY, better voice quality).
TTS_PROVIDER = os.environ.get("TTS_PROVIDER", "edge")

TTS_VOICE = os.environ.get("TTS_VOICE", "ru-RU-DmitryNeural")
TTS_VOICE_ALT = os.environ.get("TTS_VOICE_ALT", "ru-RU-SvetlanaNeural")
TTS_RATE = os.environ.get("TTS_RATE", "+0%")
TTS_VOLUME = os.environ.get("TTS_VOLUME", "+0%")
TTS_PITCH = os.environ.get("TTS_PITCH", "+0Hz")

ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
# Default voice id is ElevenLabs' own multilingual sample voice; override with
# a voice_id from your ElevenLabs Voice Library once you've picked one you like.
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "")

# Lower stability = more natural pitch/pace variation (less flat/robotic);
# style adds expressive delivery. ElevenLabs' own client defaults skew toward
# higher stability (more monotone but more consistent) than this.
ELEVENLABS_STABILITY = float(os.environ.get("ELEVENLABS_STABILITY", "0.35"))
ELEVENLABS_SIMILARITY_BOOST = float(os.environ.get("ELEVENLABS_SIMILARITY_BOOST", "0.8"))
ELEVENLABS_STYLE = float(os.environ.get("ELEVENLABS_STYLE", "0.35"))

# Numeric device id or a name substring, passed straight to
# `worker.py console --input-device/--output-device`. Needed whenever
# Windows' *default* recording/playback device isn't the one you actually
# want (e.g. a virtual mixer device like SteelSeries Sonar that silently
# swallows audio unless its passthrough is configured). Leave blank to use
# whatever Windows currently has set as default.
AUDIO_INPUT_DEVICE = os.environ.get("AUDIO_INPUT_DEVICE", "")
AUDIO_OUTPUT_DEVICE = os.environ.get("AUDIO_OUTPUT_DEVICE", "")
# eleven_v3_conversational would let Jarvis laugh/sigh/etc. via inline audio
# tags, but a live test broke the real-time decoder (av.error.InvalidDataError
# mid-conversation) -- something about how it streams doesn't match what
# livekit-agents' audio decoder expects here, unresolved. Staying on
# turbo_v2_5 (no audio-tag support, but known-working) until that's fixed.
ELEVENLABS_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_turbo_v2_5")

# Named ElevenLabs voices Jarvis can switch to by voice command
# (tools/voice_control.py). name -> ElevenLabs voice_id, matched
# case-insensitively. Lives in a data file (not hard-coded here) because
# voice ids belong to *your* ElevenLabs account -- the control panel
# (panel.py) adds/removes them, and change_voice re-reads the file on every
# call, so edits apply without a restart.
VOICE_PRESETS_FILE = DATA_DIR / "voice_presets.json"


def load_voice_presets() -> dict[str, str]:
    import json

    try:
        raw = json.loads(VOICE_PRESETS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k).strip().lower(): str(v).strip() for k, v in raw.items() if str(k).strip() and str(v).strip()}


# tools/claude_cli.py routes memory consolidation / pattern learning through
# the local `claude` CLI (which bills against a Claude.ai subscription
# instead of the API key). Off by default: whether a consumer subscription
# may be used as a backend for another application's background calls is
# Anthropic's terms to interpret, not something to switch on for people who
# installed this from GitHub. Set USE_CLAUDE_CLI=1 only if you've checked
# that it's OK for your account.
USE_CLAUDE_CLI = os.environ.get("USE_CLAUDE_CLI", "0").strip().lower() in ("1", "true", "yes", "on")
# Persists the last voice picked by voice command across restarts --
# worker.py's _build_tts() reads this before falling back to
# ELEVENLABS_VOICE_ID above.
TTS_VOICE_STATE_FILE = DATA_DIR / "tts_voice.json"

# --- Wake hotkey / auto-sleep (desktop app, worker.py console) ---
# Global hotkey (via the `keyboard` package) that wakes the agent from sleep.
WAKE_HOTKEY = os.environ.get("WAKE_HOTKEY", "f10")
# Global hotkey that turns the microphone off / back on (mic_control.py).
MIC_HOTKEY = os.environ.get("MIC_HOTKEY", "f9")

# --- HUD face with lip sync (lipsync_bridge.py -> compact_bar.FacePanel) ---
# While Jarvis speaks, a face slides out from under the status bar and its
# mouth follows the audio actually being played. HUD_FACE=0 turns it off.
HUD_FACE = os.environ.get("HUD_FACE", "1") not in ("0", "false", "False", "")
LIPSYNC_PORT = int(os.environ.get("LIPSYNC_PORT", "48123"))
# "core" -- (default) living holographic core: a voice-reactive particle sphere in HUD rings (hud_core.py);
# "head3d" -- volumetric talking hologram head, always in the bottom-right corner instead of the bar (hud_head3d.py);
# "humanoid" -- faceless hologram with a burning core, streams out of an orb (hud_humanoid.py);
# "orbs" -- the portrait as a cloud of glowing orbs; "photo" -- the realistic portrait
# (both need scripts/make_avatar.py's images).
HUD_FACE_STYLE = os.environ.get("HUD_FACE_STYLE", "core")
# After this many seconds with no speech from either side, the agent mutes
# its microphone input ("sleep") until WAKE_HOTKEY is pressed again.
SLEEP_AFTER_SILENCE_S = float(os.environ.get("SLEEP_AFTER_SILENCE_S", "180"))

OPEN_METEO_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# --- Spoken wake word while asleep (wake_word.py) ---
# Local openWakeWord model ("hey jarvis"), no network and no LLM: while the
# agent sleeps, saying "Hey Jarvis" wakes it just like WAKE_HOTKEY does.
# WAKE_WORD_ENABLED=0 turns it off; THRESHOLD is the model score (0..1) that
# counts as a detection -- raise it if it wakes up on its own.
WAKE_WORD_ENABLED = os.environ.get("WAKE_WORD_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
WAKE_WORD_MODEL = os.environ.get("WAKE_WORD_MODEL", "hey_jarvis")
WAKE_WORD_THRESHOLD = float(os.environ.get("WAKE_WORD_THRESHOLD", "0.5"))

# --- HUD panel: face + status + subtitles + day plan on one page (hud_panel.py) ---
# HUD_PANEL_AUTO=1: with a second monitor connected, the panel opens full
# screen there when Jarvis starts, and the corner face hides while it's open.
HUD_PANEL_AUTO = os.environ.get("HUD_PANEL_AUTO", "1").strip().lower() in ("1", "true", "yes", "on")
HUD_PANEL_MONITOR = int(os.environ.get("HUD_PANEL_MONITOR", "2"))
HUD_PANEL_PORT = int(os.environ.get("HUD_PANEL_PORT", "48125"))
# HUD_WALLPAPER=1: Jarvis lives in Wallpaper Engine instead (scripts/install_wallpapers.py): the
# face on the main monitor's wallpaper, the day plan on the second's. "Открой план" then just
# clears the windows off the plan monitor so the wallpaper shows.
HUD_WALLPAPER = os.environ.get("HUD_WALLPAPER", "0").strip().lower() in ("1", "true", "yes", "on")

# tools/monitors.py look_at_screen: one cheap vision call per look.
LOOK_SCREEN_MODEL = os.environ.get("LOOK_SCREEN_MODEL", ANTHROPIC_MODEL)

# --- Briefing / news (tools/briefing.py) ---
# City for the morning briefing's weather when the user didn't name one
# (memory's "city" fact is tried first).
HOME_CITY = os.environ.get("HOME_CITY", "")
# RSS/Atom feeds for get_news, comma-separated "name=url" or bare urls.
# Defaults are ones reachable from Ukraine (Russian outlets like ria.ru /
# lenta.ru time out there); an unreachable feed is just skipped.
NEWS_FEEDS = os.environ.get(
    "NEWS_FEEDS",
    "УНИАН=https://rss.unian.net/site/news_rus.rss,"
    "РБК-Украина=https://www.rbc.ua/static/rss/all.rus.rss.xml,"
    "BBC Русская служба=https://feeds.bbci.co.uk/russian/rss.xml,"
    "Хабр=https://habr.com/ru/rss/news/?fl=ru",
)

# --- Smart home: Home Assistant (tools/smart_home.py) ---
# Base URL (e.g. http://homeassistant.local:8123) and a long-lived access
# token from your HA profile page. Both empty = the tool reports "not set up".
HOME_ASSISTANT_URL = os.environ.get("HOME_ASSISTANT_URL", "").rstrip("/")
HOME_ASSISTANT_TOKEN = os.environ.get("HOME_ASSISTANT_TOKEN", "")

# --- Bluetooth LED strip, "Lotus Lantern" app (tools/led_strip.py) ---
# MAC address of the strip; empty = find it by name over Bluetooth and remember it.
LED_STRIP_ADDRESS = os.environ.get("LED_STRIP_ADDRESS", "").strip()

# --- E-mail (tools/email_tools.py) ---
# IMAP/SMTP with an *app password* (Gmail/Yandex/Mail.ru all issue them).
# Reading is free and read-only; sending needs a spoken confirmation.
EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS", "")
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD", "")
EMAIL_IMAP_HOST = os.environ.get("EMAIL_IMAP_HOST", "")
EMAIL_SMTP_HOST = os.environ.get("EMAIL_SMTP_HOST", "")
EMAIL_SMTP_PORT = int(os.environ.get("EMAIL_SMTP_PORT", "465"))

# ---------------------------------------------------------------------------
# System-action whitelist. `system_control` refuses anything not listed here.
# ---------------------------------------------------------------------------
SAFE_SYSTEM_ACTIONS = ("volume", "brightness", "wifi", "bluetooth", "lock", "mute", "sleep", "shutdown", "restart")

# Actions that are reversible / low-risk enough to run without a spoken
# "да, подтверждаю" from the user. Anything in SAFE_SYSTEM_ACTIONS but NOT
# listed here (sleep, shutdown, restart) requires confirm=True.
SYSTEM_ACTIONS_NO_CONFIRM = ("volume", "brightness", "wifi", "bluetooth", "lock", "mute")

# ---------------------------------------------------------------------------
# Application aliases for open_application / close_application.
# Extend freely -- keys are matched case-insensitively against the spoken name.
# ---------------------------------------------------------------------------
APP_ALIASES: dict[str, dict[str, str]] = {
    "chrome": {"Windows": "chrome", "Darwin": "Google Chrome", "Linux": "google-chrome"},
    "google chrome": {"Windows": "chrome", "Darwin": "Google Chrome", "Linux": "google-chrome"},
    "firefox": {"Windows": "firefox", "Darwin": "Firefox", "Linux": "firefox"},
    "edge": {"Windows": "msedge", "Darwin": "Microsoft Edge", "Linux": "microsoft-edge"},
    "vscode": {"Windows": "code", "Darwin": "Visual Studio Code", "Linux": "code"},
    "vs code": {"Windows": "code", "Darwin": "Visual Studio Code", "Linux": "code"},
    "visual studio code": {"Windows": "code", "Darwin": "Visual Studio Code", "Linux": "code"},
    "telegram": {"Windows": "Telegram", "Darwin": "Telegram", "Linux": "telegram"},
    "discord": {"Windows": "Discord", "Darwin": "Discord", "Linux": "discord"},
    "slack": {"Windows": "Slack", "Darwin": "Slack", "Linux": "slack"},
    "zoom": {"Windows": "Zoom", "Darwin": "zoom.us", "Linux": "zoom"},
    "teams": {"Windows": "msteams", "Darwin": "Microsoft Teams", "Linux": "teams"},
    "spotify": {"Windows": "spotify", "Darwin": "Spotify", "Linux": "spotify"},
    "explorer": {"Windows": "explorer.exe", "Darwin": "Finder", "Linux": "nautilus"},
    "file explorer": {"Windows": "explorer.exe", "Darwin": "Finder", "Linux": "nautilus"},
    "terminal": {"Windows": "wt", "Darwin": "Terminal", "Linux": "gnome-terminal"},
    "cmd": {"Windows": "cmd.exe", "Darwin": "Terminal", "Linux": "bash"},
    "powershell": {"Windows": "powershell.exe", "Darwin": "Terminal", "Linux": "bash"},
    "notepad": {"Windows": "notepad.exe", "Darwin": "TextEdit", "Linux": "gedit"},
    "calculator": {"Windows": "calc.exe", "Darwin": "Calculator", "Linux": "gnome-calculator"},
    "word": {"Windows": "winword", "Darwin": "Microsoft Word", "Linux": "libreoffice --writer"},
    "excel": {"Windows": "excel", "Darwin": "Microsoft Excel", "Linux": "libreoffice --calc"},
    "outlook": {"Windows": "outlook", "Darwin": "Microsoft Outlook", "Linux": "thunderbird"},
    "obsidian": {"Windows": "Obsidian", "Darwin": "Obsidian", "Linux": "obsidian"},
    "steam": {"Windows": "steam", "Darwin": "Steam", "Linux": "steam"},
    "photoshop": {"Windows": "Photoshop.exe", "Darwin": "Adobe Photoshop 2025", "Linux": "photoshop"},
    "premiere": {"Windows": "Adobe Premiere Pro.exe", "Darwin": "Adobe Premiere Pro 2025", "Linux": "premiere"},
    "premiere pro": {"Windows": "Adobe Premiere Pro.exe", "Darwin": "Adobe Premiere Pro 2025", "Linux": "premiere"},
    "after effects": {"Windows": "AfterFX.exe", "Darwin": "Adobe After Effects 2025", "Linux": "afterfx"},
    "illustrator": {"Windows": "Illustrator.exe", "Darwin": "Adobe Illustrator 2025", "Linux": "illustrator"},
}

# Process-name substrings used by close_application to match a running
# process when the friendly name doesn't match the exe name directly.
APP_PROCESS_HINTS: dict[str, str] = {
    "chrome": "chrome",
    "vscode": "code",
    "vs code": "code",
    "visual studio code": "code",
    "telegram": "telegram",
    "discord": "discord",
    "slack": "slack",
    "spotify": "spotify",
    "word": "winword",
    "excel": "excel",
    "outlook": "outlook",
}

# Directories find_and_open_file searches, in priority order.
FILE_SEARCH_DIRS = [
    Path.home() / "Desktop",
    Path.home() / "Documents",
    Path.home() / "Downloads",
]
FILE_SEARCH_MAX_RESULTS = 5

# Directories tools/coding_agent.py searches for an existing project folder
# by name. Override PROJECTS_DIR in .env if your projects live elsewhere --
# this defaults to the actual folder this user keeps their coding projects
# in, not a generic guess.
PROJECTS_DIR = Path(os.environ.get("PROJECTS_DIR", str(Path.home() / "Projects")))
PROJECT_SEARCH_DIRS = [d for d in (PROJECTS_DIR, Path.home() / "Desktop") if d.exists()]

# Model the `claude` CLI session tools/coding_agent.py launches uses --
# explicit full name rather than the "sonnet" alias so it doesn't silently
# drift to a newer Sonnet snapshot later.
CODING_AGENT_MODEL = os.environ.get("CODING_AGENT_MODEL", "claude-sonnet-5")
# coding_agent "start": run Claude Code headless in the background with its
# permissions granted up front (no window, no prompts; destructive commands
# in CLAUDE_CODE_DISALLOWED_TOOLS stay blocked). 0 = the old visible
# interactive terminal with Claude Code's own approval prompts.
CODING_AGENT_BACKGROUND = os.environ.get("CODING_AGENT_BACKGROUND", "1").strip().lower() in ("1", "true", "yes", "on")
CODING_AGENT_TIMEOUT_S = float(os.environ.get("CODING_AGENT_TIMEOUT_S", "3600"))
FILE_SEARCH_MAX_SCAN = 20000  # safety cap on number of files walked

# ---------------------------------------------------------------------------
# run_predefined_script whitelist. The LLM can only pick a `script_name` key
# below -- it can never supply its own path, interpreter, or arguments.
# ---------------------------------------------------------------------------
SCRIPT_WHITELIST: dict[str, dict[str, object]] = {
    # "backup_notes": {
    #     "description": "Copy the notes folder to the backup drive",
    #     "executable": "python",
    #     "args": [str(BASE_DIR / "scripts" / "backup_notes.py")],
    #     "timeout": 120,
    # },
}
