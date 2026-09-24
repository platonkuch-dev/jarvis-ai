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
# First sender to message the account claims this file and is the only one
# who gets real tool access from then on; everyone else gets plain
# conversation with no tools and no injected personal memory. Delete the
# file to let a new sender re-claim ownership.
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
# Longest side of the screenshots sent to the model. 1920 (~1080p) is the
# recommended accuracy/cost balance; 1280 made small UI text hard to read on a
# 1440p monitor. Sent as JPEG, and only the newest 3 are kept in the history.
COMPUTER_USE_MAX_IMAGE_DIM = int(os.environ.get("COMPUTER_USE_MAX_IMAGE_DIM", "1920"))
COMPUTER_USE_LOG_FILE = LOGS_DIR / "computer_use.log"

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
# After this many seconds with no speech from either side, the agent mutes
# its microphone input ("sleep") until WAKE_HOTKEY is pressed again.
SLEEP_AFTER_SILENCE_S = float(os.environ.get("SLEEP_AFTER_SILENCE_S", "180"))

OPEN_METEO_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# ---------------------------------------------------------------------------
# System-action whitelist. `system_control` refuses anything not listed here.
# ---------------------------------------------------------------------------
SAFE_SYSTEM_ACTIONS = ("volume", "brightness", "wifi", "bluetooth", "lock", "sleep")

# Actions that are reversible / low-risk enough to run without a spoken
# "да, подтверждаю" from the user. Anything in SAFE_SYSTEM_ACTIONS but NOT
# listed here (currently just "sleep") requires confirm=True.
SYSTEM_ACTIONS_NO_CONFIRM = ("volume", "brightness", "wifi", "bluetooth", "lock")

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
