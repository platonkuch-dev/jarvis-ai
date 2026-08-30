import platform as _platform
import subprocess as _subprocess

# ── Nuclear: force CREATE_NO_WINDOW on EVERY subprocess call on Windows ───────
# This patches Popen itself, so no per-file flag is needed anywhere.
if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)   # drop any stale/shared STARTUPINFO
            super().__init__(args, **kw)

    _subprocess.Popen = _Popen
# ─────────────────5────────────────────────────────────────────────────────────

import sys

# ── Force UTF-8 on stdout/stderr ──────────────────────────────────────────────
# print() statements throughout this codebase use emoji (🎤 👂 🔊 ...). On a
# non-English Windows locale (e.g. Russian, codepage 1251), and especially in
# a PyInstaller windowed build where there's no real console to negotiate an
# encoding with, Python falls back to the ANSI codepage — which can't encode
# those characters and raises UnicodeEncodeError, crashing whichever async
# task tried to log. That takes down the whole Gemini Live session with it.
# errors="replace" makes logging degrade gracefully instead of crashing.
#
# line_buffering=True matters separately from the encoding fix: stdout is
# fully-buffered (not line-buffered) whenever it isn't a real TTY — exactly
# the case when JARVIS is launched with output redirected to a log file for
# diagnostics. Without it, print() output sits in an internal buffer for a
# long time before actually reaching the file, making a healthy process look
# stalled/silent when it isn't (same root cause diagnosed and fixed in
# voice/fast_path_demo.py — never applied here until now).
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass

import asyncio
import threading
import time
import json
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np
from google import genai
from google.genai import types
from ui import JarvisUI
from core.path_utils import resource_path
from core import runtime_config, latency
from voice.pipeline import VoicePipeline
from voice.intent_classifier import IntentClassifier
from memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
)
from memory.pattern_learning import log_tool_call
from core.config import GEMINI_LIVE_MODEL as LIVE_MODEL, GEMINI_VOICE_NAME

from actions.file_processor import file_processor
from actions.flight_finder     import flight_finder
from actions.weather_report    import weather_action
from actions.send_message      import send_message
from actions.scheduled_send    import schedule_message
from actions.reminder          import reminder
from actions.computer_settings import computer_settings
from actions.screen_processor  import _capture_camera, _capture_screen, capture_screen_full
from actions.screen_watch      import screen_watch, get_latest_frame as _get_watched_frame
from actions.youtube_video     import youtube_video
from actions.desktop           import desktop_control
from actions.browser_control   import browser_control
from actions.file_controller   import file_controller
from actions.code_helper       import code_helper
from actions.dev_agent         import dev_agent
from actions.web_search        import web_search as web_search_action
from actions.computer_control  import computer_control
from actions.game_updater      import game_updater
from actions.system_monitor    import SystemMonitor, get_system_status
from actions.proactive         import ProactiveEngine
from actions.window_control    import window_manager, ui_automation, launch_and_verify

# Reconnect-loop support (API key lookup, TaskGroup exception classification,
# system-prompt/transcript loading) -- moved to core/session.py in the Stage 2
# module split (see REWORK_PLAN.md); re-imported here under their original
# private names so every call site below is unchanged.
from core.session import (
    PROMPT_PATH,
    get_api_key as _get_api_key,
    flatten_exceptions as _flatten_exceptions,
    is_audio_device_error as _is_audio_device_error,
    load_system_prompt as _load_system_prompt,
    clean_transcript as _clean_transcript,
)
# Audio pipeline (mic/speaker I/O, response handling) and Fast Path (local
# wake word/STT/router/TTS) -- moved to core/audio_pipeline.py and
# core/fast_path.py in the Stage 2 module split (see REWORK_PLAN.md).
# JarvisLive keeps thin `_foo` wrapper methods that delegate into these.
from core import audio_pipeline, fast_path
from core.audio_pipeline import CHANNELS, SEND_SAMPLE_RATE, RECEIVE_SAMPLE_RATE, CHUNK_SIZE


BASE_DIR        = Path(__file__).resolve().parent

TOOL_DECLARATIONS = [
    {
        "name": "open_app",
        "description": (
            "Opens any application on the computer. "
            "Use this whenever the user asks to open, launch, or start any app, "
            "website, or program. Always call this tool — never just say you opened it."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "app_name": {
                    "type": "STRING",
                    "description": "Exact name of the application (e.g. 'WhatsApp', 'Chrome', 'Spotify')"
                }
            },
            "required": ["app_name"]
        }
    },
    {
        "name": "web_search",
        "description": (
            "Searches the web. Use for ANY question about current facts, events, prices, "
            "or topics — always prefer this over guessing. "
            "Modes: 'search' (default), 'news' (latest headlines on a topic), "
            "'research' (deep comprehensive answer), 'price' (product cost lookup), "
            "'compare' (side-by-side comparison of items)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query":  {"type": "STRING", "description": "Search query or topic"},
                "mode":   {"type": "STRING", "description": "search | news | research | price | compare"},
                "items":  {"type": "ARRAY",  "items": {"type": "STRING"}, "description": "Items to compare (compare mode)"},
                "aspect": {"type": "STRING", "description": "Comparison aspect: price | specs | reviews | features"},
            },
            "required": ["query"]
        }
    },
    {
        "name": "system_status",
        "description": (
            "Returns real-time system metrics: CPU usage, RAM, GPU load, CPU temperature, "
            "uptime, and process count. Use when the user asks about computer performance, "
            "temperature, memory, or resource usage."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "weather_report",
        "description": "Gives the weather report to user",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "city": {"type": "STRING", "description": "City name"}
            },
            "required": ["city"]
        }
    },
    {
        "name": "send_message",
        "description": (
            "Sends a text message via WhatsApp, Telegram, or other messaging platform. "
            "For Telegram, the recipient can be anyone (username, phone, numeric id, or a "
            "free-form contact name like 'Катя') — not just the authorized user. "
            "Pass 'date' and 'time' together to schedule a Telegram message for later "
            "instead of sending it immediately — it will go out at that time even if "
            "JARVIS isn't running. "
            "Pass 'voice'=true to have Telegram deliver it as a spoken voice message "
            "instead of text (synthesized from message_text)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "receiver":     {"type": "STRING", "description": "Recipient contact name, @username, phone, or numeric id"},
                "message_text": {"type": "STRING", "description": "The message to send"},
                "platform":     {"type": "STRING", "description": "Platform: WhatsApp, Telegram, etc."},
                "date":         {"type": "STRING", "description": "Optional: YYYY-MM-DD — schedule instead of sending now (Telegram only, requires 'time' too)"},
                "time":         {"type": "STRING", "description": "Optional: HH:MM 24h — schedule instead of sending now (Telegram only, requires 'date' too)"},
                "voice":        {"type": "BOOLEAN", "description": "Optional: true = send as a spoken voice message instead of text (Telegram only, immediate sends only)"}
            },
            "required": ["receiver", "message_text", "platform"]
        }
    },
    {
        "name": "reminder",
        "description": "Sets a timed reminder using Task Scheduler.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "date":    {"type": "STRING", "description": "Date in YYYY-MM-DD format"},
                "time":    {"type": "STRING", "description": "Time in HH:MM format (24h)"},
                "message": {"type": "STRING", "description": "Reminder message text"}
            },
            "required": ["date", "time", "message"]
        }
    },
    {
        "name": "youtube_video",
        "description": (
            "Controls YouTube. Use for: playing videos, summarizing a video's content, "
            "getting video info, or showing trending videos."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "play | summarize | get_info | trending (default: play)"},
                "query":  {"type": "STRING", "description": "Search query for play action"},
                "save":   {"type": "BOOLEAN", "description": "Save summary to Notepad (summarize only)"},
                "region": {"type": "STRING", "description": "Country code for trending e.g. TR, US"},
                "url":    {"type": "STRING", "description": "Video URL for get_info action"},
            },
            "required": []
        }
    },
    {
        "name": "send_screenshot",
        "description": (
            "Captures a screenshot of the PC screen and SENDS the actual image "
            "file to the current Telegram chat. Use this whenever the user asks "
            "to send/share/forward a screenshot or a picture of the screen — as "
            "opposed to screen_process, which only describes what's on screen "
            "out loud and does not deliver a file. Only works for requests that "
            "came in via Telegram."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
    },
    {
        "name": "screen_process",
        "description": (
            "Captures the screen or webcam image and lets you analyze it. "
            "MUST be called when user asks what is on screen, what you see, "
            "look at camera, analyze my screen, etc. "
            "You have NO visual ability without this tool. "
            "After the image is captured it is sent directly to you — describe what you see and answer the user's question. "
            "When using camera: the live view stays open until user says close it or calls close_camera."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {"type": "STRING", "description": "'screen' to capture display, 'camera' for webcam. Default: 'screen'"},
                "text":  {"type": "STRING", "description": "The question or instruction about the captured image"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "screen_watch",
        "description": (
            "Starts or stops a background mode where a fresh screenshot is taken every ~12 seconds "
            "and kept ready in memory, so screen_process answers instantly without waiting on a fresh "
            "capture. Does NOT analyze or narrate anything by itself — it just keeps a recent frame ready. "
            "Call action='start' only when the user explicitly asks you to watch/keep an eye on their "
            "screen (e.g. 'watch my screen', 'следи за экраном'). Call action='stop' when they say to "
            "stop. Call action='status' if asked whether you're currently watching."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "start | stop | status"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "close_camera",
        "description": (
            "Closes the live camera view shown on screen. "
            "Call when user says: close camera, stop camera, turn off camera, "
            "kamerayı kapat, kapat, creepy, etc."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
    },
    {
        "name": "computer_settings",
        "description": (
            "Controls the computer: volume, brightness, window management, keyboard shortcuts, "
            "typing text on screen, closing apps, fullscreen, dark mode, WiFi, restart, shutdown, "
            "scrolling, tab management, zoom, screenshots, lock screen, refresh/reload page. "
            "Use for ANY single computer control command."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "The action to perform"},
                "description": {"type": "STRING", "description": "Natural language description of what to do"},
                "value":       {"type": "STRING", "description": "Optional value: volume level, text to type, etc."}
            },
            "required": []
        }
    },
    {
        "name": "browser_control",
        "description": (
            "Controls any web browser. Use for: opening websites, searching the web, "
            "clicking elements, filling forms, scrolling, screenshots, navigation, any web-based task. "
            "Always pass the 'browser' parameter when the user specifies a browser (e.g. 'open in Edge', "
            "'use Firefox', 'open Chrome'). Multiple browsers can run simultaneously."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "go_to | search | click | type | scroll | fill_form | smart_click | smart_type | get_text | get_url | press | new_tab | close_tab | screenshot | back | forward | reload | switch | list_browsers | close | close_all"},
                "browser":     {"type": "STRING", "description": "Target browser: chrome | edge | firefox | opera | operagx | brave | vivaldi | safari. Omit to use the currently active browser."},
                "url":         {"type": "STRING", "description": "URL for go_to / new_tab action"},
                "query":       {"type": "STRING", "description": "Search query for search action"},
                "engine":      {"type": "STRING", "description": "Search engine: google | bing | duckduckgo | yandex (default: google)"},
                "selector":    {"type": "STRING", "description": "CSS selector for click/type"},
                "text":        {"type": "STRING", "description": "Text to click or type"},
                "description": {"type": "STRING", "description": "Element description for smart_click/smart_type"},
                "direction":   {"type": "STRING", "description": "up | down for scroll"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount in pixels (default: 500)"},
                "key":         {"type": "STRING", "description": "Key name for press action (e.g. Enter, Escape, F5)"},
                "path":        {"type": "STRING", "description": "Save path for screenshot"},
                "incognito":   {"type": "BOOLEAN", "description": "Open in private/incognito mode"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "file_controller",
        "description": "Manages files and folders: list, create, delete, move, copy, rename, read, write, find, disk usage.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "list | create_file | create_folder | delete | move | copy | rename | read | write | find | largest | disk_usage | organize_desktop | info"},
                "path":        {"type": "STRING", "description": "File/folder path or shortcut: desktop, downloads, documents, home"},
                "destination": {"type": "STRING", "description": "Destination path for move/copy"},
                "new_name":    {"type": "STRING", "description": "New name for rename"},
                "content":     {"type": "STRING", "description": "Content for create_file/write"},
                "name":        {"type": "STRING", "description": "File name to search for"},
                "extension":   {"type": "STRING", "description": "File extension to search (e.g. .pdf)"},
                "count":       {"type": "INTEGER", "description": "Number of results for largest"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "desktop_control",
        "description": "Controls the desktop: wallpaper, organize, clean, list, stats.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "wallpaper | wallpaper_url | organize | clean | list | stats | task"},
                "path":   {"type": "STRING", "description": "Image path for wallpaper"},
                "url":    {"type": "STRING", "description": "Image URL for wallpaper_url"},
                "mode":   {"type": "STRING", "description": "by_type or by_date for organize"},
                "task":   {"type": "STRING", "description": "Natural language desktop task"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "code_helper",
        "description": "Writes, edits, explains, runs, or builds code files. Can use Gemini or Claude as the underlying model.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "write | edit | explain | run | build | auto (default: auto)"},
                "description": {"type": "STRING", "description": "What the code should do or what change to make"},
                "language":    {"type": "STRING", "description": "Programming language (default: python)"},
                "output_path": {"type": "STRING", "description": "Where to save the file"},
                "file_path":   {"type": "STRING", "description": "Path to existing file for edit/explain/run/build"},
                "code":        {"type": "STRING", "description": "Raw code string for explain"},
                "args":        {"type": "STRING", "description": "CLI arguments for run/build"},
                "timeout":     {"type": "INTEGER", "description": "Execution timeout in seconds (default: 30)"},
                "provider":    {"type": "STRING", "description": "Which model writes the code: 'claude' (default) or 'gemini'. Use 'gemini' only if the user explicitly asks for Gemini."},
            },
            "required": ["action"]
        }
    },
    {
        "name": "dev_agent",
        "description": "Builds complete multi-file projects from scratch: plans, writes files, installs deps, opens VSCode, runs and fixes errors. Can use Gemini or Claude as the underlying model.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "description":  {"type": "STRING", "description": "What the project should do"},
                "language":     {"type": "STRING", "description": "Programming language (default: python)"},
                "project_name": {"type": "STRING", "description": "Optional project folder name"},
                "timeout":      {"type": "INTEGER", "description": "Run timeout in seconds (default: 30)"},
                "provider":     {"type": "STRING", "description": "Which model plans/writes the project: 'claude' (default) or 'gemini'. Use 'gemini' only if the user explicitly asks for Gemini."},
            },
            "required": ["description"]
        }
    },
    {
        "name": "computer_control",
        "description": "Direct computer control: type, click, hotkeys, scroll, move mouse, screenshots, find elements on screen.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "type | smart_type | click | double_click | right_click | hotkey | press | scroll | move | copy | paste | screenshot | wait | clear_field | focus_window | screen_find | screen_click | random_data | user_data"},
                "text":        {"type": "STRING", "description": "Text to type or paste"},
                "x":           {"type": "INTEGER", "description": "X coordinate"},
                "y":           {"type": "INTEGER", "description": "Y coordinate"},
                "keys":        {"type": "STRING", "description": "Key combination e.g. 'ctrl+c'"},
                "key":         {"type": "STRING", "description": "Single key e.g. 'enter'"},
                "direction":   {"type": "STRING", "description": "up | down | left | right"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount (default: 3)"},
                "seconds":     {"type": "NUMBER",  "description": "Seconds to wait"},
                "title":       {"type": "STRING",  "description": "Window title for focus_window"},
                "description": {"type": "STRING",  "description": "Element description for screen_find/screen_click"},
                "type":        {"type": "STRING",  "description": "Data type for random_data"},
                "field":       {"type": "STRING",  "description": "Field for user_data: name|email|city"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
                "path":        {"type": "STRING",  "description": "Save path for screenshot"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "window_manager",
        "description": (
            "Manages open windows and running applications by name — more precise than "
            "computer_settings because it targets a SPECIFIC app instead of whatever "
            "window happens to be active. Use for: switching to/focusing an app "
            "('переключись на Discord', 'switch to Chrome'), closing a specific app "
            "('закрой Discord'), minimizing/maximizing/restoring a specific window, "
            "moving/resizing a window, listing open windows or running processes, or "
            "waiting for an app's window to appear after launching it. "
            "For OPENING/LAUNCHING an app use open_app instead — this tool only manages "
            "windows that already exist (or are expected to appear soon)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":  {"type": "STRING", "description": "list_windows | list_processes | get_active | focus | minimize | maximize | restore | close | move | resize | wait_for_window"},
                "app":     {"type": "STRING", "description": "App name or window title fragment, e.g. 'Discord', 'Chrome'. Omit to act on the last-focused app/window."},
                "x":       {"type": "INTEGER", "description": "Target X position for move"},
                "y":       {"type": "INTEGER", "description": "Target Y position for move"},
                "width":   {"type": "INTEGER", "description": "Target width for resize"},
                "height":  {"type": "INTEGER", "description": "Target height for resize"},
                "timeout": {"type": "NUMBER",  "description": "Seconds to wait for wait_for_window (default 10)"},
                "force":   {"type": "BOOLEAN", "description": "Force-close (kill) if the app doesn't close gracefully (default false — a normal close is tried first)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "ui_automation",
        "description": (
            "Finds and interacts with controls INSIDE an application window — buttons, "
            "text fields, checkboxes, menus, lists, tabs — using Windows UI Automation, "
            "with an automatic fallback to on-screen visual search if a control isn't "
            "exposed to accessibility. Use whenever the user names a specific on-screen "
            "control rather than giving raw coordinates: 'нажми кнопку Settings', 'найди "
            "кнопку Login', 'напиши в поле поиска Minecraft', 'выбери пункт меню Export'. "
            "For blind typing/clicking with no named target, use computer_control instead."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":       {"type": "STRING", "description": "find | click | double_click | right_click | type | read | select | clear | get_tree | wait_for_element"},
                "app":          {"type": "STRING", "description": "App/window to scope the search to. Omit to use the last-focused app."},
                "query":        {"type": "STRING", "description": "Name/text/label of the element to find or act on, e.g. 'Settings', 'Search', 'Send'"},
                "control_type": {"type": "STRING", "description": "Optional filter: Button | Edit | Text | CheckBox | RadioButton | ComboBox | List | ListItem | Menu | MenuItem | Tab | TabItem | Tree | TreeItem | Window | Pane | Hyperlink"},
                "text":         {"type": "STRING", "description": "Text to type (type action)"},
                "item":         {"type": "STRING", "description": "Item name to select, for combo/list boxes (select action)"},
                "clear_first":  {"type": "BOOLEAN", "description": "Clear the field before typing (default true)"},
                "index":        {"type": "INTEGER", "description": "Which match to use if several elements match the same query (default 0, the best match)"},
                "max_depth":    {"type": "INTEGER", "description": "UI tree depth limit for get_tree (default 3)"},
                "timeout":      {"type": "NUMBER",  "description": "Seconds to wait for wait_for_element (default 8)"},
                "x":            {"type": "INTEGER", "description": "Optional literal screen X — skips element search and clicks directly (click action)"},
                "y":            {"type": "INTEGER", "description": "Optional literal screen Y — skips element search and clicks directly (click action)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "game_updater",
        "description": (
            "THE ONLY tool for ANY Steam or Epic Games request. "
            "Use for: installing, downloading, updating games, listing installed games, "
            "checking download status, scheduling updates. "
            "ALWAYS call directly for any Steam/Epic/game request. "
            "NEVER use browser_control or web_search for Steam/Epic."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":    {"type": "STRING",  "description": "update | install | list | download_status | schedule | cancel_schedule | schedule_status (default: update)"},
                "platform":  {"type": "STRING",  "description": "steam | epic | both (default: both)"},
                "game_name": {"type": "STRING",  "description": "Game name (partial match supported)"},
                "app_id":    {"type": "STRING",  "description": "Steam AppID for install (optional)"},
                "hour":      {"type": "INTEGER", "description": "Hour for scheduled update 0-23 (default: 3)"},
                "minute":    {"type": "INTEGER", "description": "Minute for scheduled update 0-59 (default: 0)"},
                "shutdown_when_done": {"type": "BOOLEAN", "description": "Shut down PC when download finishes"},
            },
            "required": []
        }
    },
    {
        "name": "flight_finder",
        "description": "Searches Google Flights and speaks the best options.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "origin":      {"type": "STRING",  "description": "Departure city or airport code"},
                "destination": {"type": "STRING",  "description": "Arrival city or airport code"},
                "date":        {"type": "STRING",  "description": "Departure date (any format)"},
                "return_date": {"type": "STRING",  "description": "Return date for round trips"},
                "passengers":  {"type": "INTEGER", "description": "Number of passengers (default: 1)"},
                "cabin":       {"type": "STRING",  "description": "economy | premium | business | first"},
                "save":        {"type": "BOOLEAN", "description": "Save results to Notepad"},
            },
            "required": ["origin", "destination", "date"]
        }
    },
    {
        "name": "shutdown_jarvis",
        "description": (
            "Shuts down the assistant completely. "
            "Call this when the user expresses intent to end the conversation, "
            "close the assistant, say goodbye, or stop Jarvis. "
            "The user can say this in ANY language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
    "name": "file_processor",
    "description": (
        "Processes any file that the user has uploaded or dropped onto the interface. "
        "Use this when the user refers to an uploaded file and wants an action on it. "
        "Supports: images (describe/ocr/resize/compress/convert), "
        "PDFs (summarize/extract_text/to_word), "
        "Word docs & text files (summarize/fix/reformat/translate), "
        "CSV/Excel (analyze/stats/filter/sort/convert), "
        "JSON/XML (validate/format/analyze), "
        "code files (explain/review/fix/optimize/run/document/test), "
        "audio (transcribe/trim/convert/info), "
        "video (trim/extract_audio/extract_frame/compress/transcribe/info), "
        "archives (list/extract), "
        "presentations (summarize/extract_text). "
        "ALWAYS call this tool when a file has been uploaded and the user gives a command about it. "
        "If the user's command is ambiguous, pick the most logical action for that file type."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "file_path": {
                "type": "STRING",
                "description": "Full path to the uploaded file. Leave empty to use the currently uploaded file."
            },
            "action": {
                "type": "STRING",
                "description": (
                    "What to do with the file. Examples by type:\n"
                    "image: describe | ocr | resize | compress | convert | info\n"
                    "pdf: summarize | extract_text | to_word | info\n"
                    "docx/txt: summarize | fix | reformat | translate_hint | word_count | to_bullet\n"
                    "csv/excel: analyze | stats | filter | sort | convert | info\n"
                    "json: validate | format | analyze | to_csv\n"
                    "code: explain | review | fix | optimize | run | document | test\n"
                    "audio: transcribe | trim | convert | info\n"
                    "video: trim | extract_audio | extract_frame | compress | transcribe | info | convert\n"
                    "archive: list | extract\n"
                    "pptx: summarize | extract_text | analyze"
                )
            },
            "instruction": {
                "type": "STRING",
                "description": "Free-form instruction if action doesn't cover it. E.g. 'translate this to Turkish', 'find all email addresses'"
            },
            "format": {
                "type": "STRING",
                "description": "Target format for conversion. E.g. 'mp3', 'pdf', 'csv', 'png'"
            },
            "width":     {"type": "INTEGER", "description": "Target width for image resize"},
            "height":    {"type": "INTEGER", "description": "Target height for image resize"},
            "scale":     {"type": "NUMBER",  "description": "Scale factor for image resize (e.g. 0.5)"},
            "quality":   {"type": "INTEGER", "description": "Quality 1-100 for image/video compress"},
            "start":     {"type": "STRING",  "description": "Start time for trim: seconds or HH:MM:SS"},
            "end":       {"type": "STRING",  "description": "End time for trim: seconds or HH:MM:SS"},
            "timestamp": {"type": "STRING",  "description": "Timestamp for video frame extraction HH:MM:SS"},
            "column":    {"type": "STRING",  "description": "Column name for CSV filter/sort"},
            "value":     {"type": "STRING",  "description": "Filter value for CSV filter"},
            "condition": {"type": "STRING",  "description": "Filter condition: equals|contains|gt|lt"},
            "ascending": {"type": "BOOLEAN", "description": "Sort order for CSV sort (default: true)"},
            "save":      {"type": "BOOLEAN", "description": "Save result to file (default: true)"},
            "destination": {"type": "STRING", "description": "Output folder for archive extract"},
        },
        "required": []
    }
},
    {
        "name": "save_memory",
        "description": (
            "Save an important personal fact about the user to long-term memory. "
            "Call this silently whenever the user reveals something worth remembering: "
            "name, age, city, job, preferences, hobbies, relationships, projects, or future plans. "
            "Do NOT call for: weather, reminders, searches, or one-time commands. "
            "Do NOT announce that you are saving — just call it silently. "
            "Values must be in English regardless of the conversation language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity — name, age, birthday, city, job, language, nationality | "
                        "preferences — favorite food/color/music/film/game/sport, hobbies | "
                        "projects — active projects, goals, things being built | "
                        "relationships — friends, family, partner, colleagues | "
                        "wishes — future plans, things to buy, travel dreams | "
                        "notes — habits, schedule, anything else worth remembering"
                    )
                },
                "key":   {"type": "STRING", "description": "Short snake_case key (e.g. name, favorite_food, sister_name)"},
                "value": {"type": "STRING", "description": "Concise value in English (e.g. Fatih, pizza, older sister)"},
            },
            "required": ["category", "key", "value"]
        }
    },
]

# --- Plugin system ---


class JarvisLive:

    def __init__(self, ui: JarvisUI):
        self.ui             = ui
        self.session              = None
        self.audio_in_queue       = None
        self.out_queue            = None
        self._loop                = None
        self._is_speaking         = False
        self._speaking_lock       = threading.Lock()
        self._phone_active        = False   # True while phone mic is streaming; pauses PC mic
        self._pending_vision       = None    # (img_bytes, mime_type, question, angle) to inject after tool response
        self._vision_cam_active    = False   # True if camera was opened for vision → auto-close after response
        self._vision_close_pending = False   # True after vision injected; next turn_complete closes camera
        self._vision_last_time     = 0.0     # monotonic time of last screen_process call (cooldown guard)
        self._vision_busy          = False   # True while a vision capture/inject cycle is in flight
        self._interrupted          = False   # True while draining audio after user interrupt
        self.ui.on_text_command   = self._on_text_command
        self.ui.on_remote_clicked = self._make_remote_key
        self.ui.on_interrupt      = self.interrupt
        self._turn_done_event: asyncio.Event | None = None
        self._dashboard     = None
        self._telegram             = None   # TelegramUserbot instance (second account)
        self._telegram_reply_target = None  # chat_id to send the next completed reply to
        self._telegram_audio_chunks: list = []  # raw PCM collected for the current Telegram-originated turn
        self._sys_monitor      = SystemMonitor()  # persistent cooldown state
        self._proactive        = ProactiveEngine()
        self._last_user_speech = time.monotonic()  # updated on every user utterance

        # Fast Path — local wake word + STT + regex router + local TTS, so
        # simple commands ("open Telegram", "volume 30") execute without a
        # round-trip to Gemini/Claude. Runs in parallel with the existing
        # Gemini Live audio stream; only takes over for the duration of one
        # detected utterance (wake word -> trailing silence), during which
        # that audio is withheld from Gemini so the same command can't get
        # double-handled by both paths. See voice/README.md.
        self._voice_pipeline      = VoicePipeline()
        self._fast_path_queue: asyncio.Queue | None = None
        self._fast_path_state     = "idle"   # idle | recording
        self._fast_path_lock      = threading.Lock()
        # MiniLM fuzzy-intent tier — sits between the regex Fast Router and
        # the Smart Path, catching paraphrases regex is too literal to match
        # (e.g. "подними звук"). Zero-arg intents only; see intent_classifier.py.
        self._intent_classifier   = IntentClassifier()

    def _make_remote_key(self):
        """Called from Qt main thread when user presses Remote Control."""
        if self._dashboard is None:
            self.ui.write_log(
                "SYS: Dashboard unavailable. "
                "Run: pip install fastapi \"uvicorn[standard]\" cryptography"
            )
            return None
        key    = self._dashboard.new_key()
        url    = self._dashboard.get_url()
        manual = self._dashboard.get_manual_url()
        return url, key, f"{url}/auto-login?key={key}", manual

    def _on_text_command(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        if value:
            self.ui.set_state("SPEAKING")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")

    def interrupt(self) -> None:
        """Stop JARVIS mid-speech: drain queued audio and open mic immediately."""
        self._interrupted = True
        q = self.audio_in_queue
        if q:
            drained = 0
            while True:
                try:
                    q.get_nowait()
                    drained += 1
                except Exception:
                    break
            if drained:
                print(f"[JARVIS] ✋ Interrupted — {drained} audio chunks discarded")
        self.set_speaking(False)
        if self._turn_done_event:
            self._turn_done_event.clear()
        self.ui.write_log("SYS: Interrupted — listening...")

    def speak(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"Sir, {tool_name} encountered an error. {short}")

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        sys_prompt = _load_system_prompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        parts = [time_ctx]
        if mem_str:
            parts.append(mem_str)
        parts.append(sys_prompt)

        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": TOOL_DECLARATIONS}],
            session_resumption=types.SessionResumptionConfig(),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=GEMINI_VOICE_NAME
                    )
                )
            ),
        )

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})
        _t_start = time.monotonic()

        print(f"[JARVIS] 🔧 {name}  {args}")
        self.ui.set_state("THINKING")
        log_tool_call(name, args)

        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            latency.record(name, (time.monotonic() - _t_start) * 1000)
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        loop   = asyncio.get_event_loop()
        result = "Done."

        try:
            if name == "open_app":
                r = await loop.run_in_executor(None, lambda: launch_and_verify(args.get("app_name", "")))
                result = r or f"Opened {args.get('app_name')}."

            elif name == "weather_report":
                r = await loop.run_in_executor(None, lambda: weather_action(parameters=args, player=self.ui))
                result = r or "Weather delivered."

            elif name == "browser_control":
                r = await loop.run_in_executor(None, lambda: browser_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "file_controller":
                r = await loop.run_in_executor(None, lambda: file_controller(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "send_message":
                platform    = (args.get("platform") or "").strip().lower()
                is_telegram = platform in ("telegram", "tg")
                if args.get("date") and args.get("time"):
                    r = await loop.run_in_executor(None, lambda: schedule_message(parameters=args, player=self.ui))
                elif is_telegram and args.get("voice") and self._telegram and self._telegram.client:
                    r = await self._telegram.send_voice_to(args.get("receiver", ""), args.get("message_text", ""))
                elif is_telegram and self._telegram and self._telegram.client:
                    r = await self._telegram.send_to(args.get("receiver", ""), args.get("message_text", ""))
                else:
                    r = await loop.run_in_executor(None, lambda: send_message(parameters=args, response=None, player=self.ui, session_memory=None))
                result = r or f"Message sent to {args.get('receiver')}."

            elif name == "reminder":
                r = await loop.run_in_executor(None, lambda: reminder(parameters=args, response=None, player=self.ui))
                result = r or "Reminder set."

            elif name == "youtube_video":
                r = await loop.run_in_executor(None, lambda: youtube_video(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "send_screenshot":
                if not (self._telegram and self._telegram_reply_target is not None):
                    result = "I can only send a screenshot to a Telegram chat — this request didn't come from Telegram."
                else:
                    target = self._telegram_reply_target
                    img_b = await loop.run_in_executor(None, capture_screen_full)
                    await self._telegram.send_photo(target, img_b, filename="screenshot.png")
                    result = "Screenshot sent to the chat."

            elif name == "screen_process":
                import time as _t_mod
                _now = _t_mod.monotonic()
                _cooldown = 4.0  # seconds — covers echo window after speaking ends
                if self._vision_busy or (_now - self._vision_last_time) < _cooldown:
                    _wait = max(0, _cooldown - (_now - self._vision_last_time))
                    print(f"[Vision] ⏳ Cooldown active ({_wait:.1f}s remaining) — ignoring duplicate call")
                    result = "Vision is still processing the previous request. I will not call this again."
                else:
                    self._vision_busy      = True
                    self._vision_last_time = _now
                    angle     = args.get("angle", "screen").lower()
                    user_text = args.get("text", "What do you see?")
                    if angle == "camera":
                        img_b, mime_t = await loop.run_in_executor(None, _capture_camera)
                        self.ui.start_camera_stream()
                        self._vision_cam_active = True
                        print(f"[Vision] 📷 Camera: {len(img_b):,} bytes")
                        _stall = "camera"
                    else:
                        watched = _get_watched_frame()
                        if watched is not None:
                            img_b, mime_t = watched
                            print(f"[Vision] 🖥️  Screen (from watch cache): {len(img_b):,} bytes")
                        else:
                            img_b, mime_t = await loop.run_in_executor(None, _capture_screen)
                            print(f"[Vision] 🖥️  Screen: {len(img_b):,} bytes")
                        _stall = "screen"
                        # Request came in via Telegram → also deliver the actual
                        # screenshot file to that chat, not just a spoken description.
                        if self._telegram and self._telegram_reply_target is not None:
                            asyncio.create_task(
                                self._telegram.send_photo(self._telegram_reply_target, img_b)
                            )
                    self._pending_vision = (img_b, mime_t, user_text, angle)
                    result = (
                        f"[VISION_ACTIVE] {_stall.capitalize()} captured. "
                        f"Immediately say ONE natural sentence in the user's language "
                        f"(e.g. 'Looking at your {_stall} now' / "
                        f"'{'Kameraya' if _stall == 'camera' else 'Ekrana'} bakıyorum'). "
                        f"Do NOT describe or guess content — the actual image arrives in the NEXT message."
                    )

            elif name == "screen_watch":
                r = await loop.run_in_executor(None, lambda: screen_watch(parameters=args))
                result = r or "Done."

            elif name == "close_camera":
                self.ui.stop_camera_stream()
                result = "Camera closed."

            elif name == "computer_settings":
                r = await loop.run_in_executor(None, lambda: computer_settings(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "desktop_control":
                r = await loop.run_in_executor(None, lambda: desktop_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "code_helper":
                r = await loop.run_in_executor(None, lambda: code_helper(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "dev_agent":
                r = await loop.run_in_executor(None, lambda: dev_agent(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "web_search":
                r = await loop.run_in_executor(None, lambda: web_search_action(parameters=args, player=self.ui))
                result = r or "Done."
                # Mirror results to the on-screen content panel
                _mode = args.get("mode", "search")
                if r and not r.startswith("No results") and not r.startswith("Search failed"):
                    _query = args.get("query") or ", ".join(args.get("items", []))
                    _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
                    self.ui.show_content(_label, r)
            elif name == "file_processor":
                if not args.get("file_path") and self.ui.current_file:
                    args["file_path"] = self.ui.current_file
                r = await loop.run_in_executor(
                    None,
                    lambda: file_processor(parameters=args, player=self.ui, speak=self.speak)
                )
                result = r or "Done."

            elif name == "computer_control":
                r = await loop.run_in_executor(None, lambda: computer_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "window_manager":
                r = await loop.run_in_executor(None, lambda: window_manager(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "ui_automation":
                r = await loop.run_in_executor(None, lambda: ui_automation(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "game_updater":
                r = await loop.run_in_executor(None, lambda: game_updater(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "flight_finder":
                r = await loop.run_in_executor(None, lambda: flight_finder(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "system_status":
                r = await loop.run_in_executor(None, get_system_status)
                result = str(r)

            elif name == "shutdown_jarvis":
                self.ui.write_log("SYS: Shutdown requested.")
                self.speak("Goodbye.")
                def _shutdown():
                    import time, os
                    time.sleep(1)
                    os._exit(0)
                threading.Thread(target=_shutdown, daemon=True).start()

            else:
                result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)
            # screen_process sets _vision_busy=True *before* capturing (see
            # above) so a concurrent duplicate call gets rejected by the
            # cooldown check. If the capture itself then raises (no camera,
            # screenshot permission error, ...), nothing else ever clears
            # that flag -- _pending_vision never gets set, so _receive_audio's
            # turn_complete handler (the normal place _vision_busy resets)
            # never runs. Without this, one failed vision call permanently
            # wedges screen_process for the rest of the session: every next
            # attempt is rejected with "still processing the previous
            # request" until a full Gemini reconnect happens to reset it.
            if name == "screen_process":
                self._vision_busy = False

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        latency.record(name, (time.monotonic() - _t_start) * 1000)
        print(f"[JARVIS] 📤 {name} → {str(result)[:80]}")
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result}
        )

    # ── Audio pipeline + Fast Path (moved to core/audio_pipeline.py and
    # core/fast_path.py in the Stage 2 module split, see REWORK_PLAN.md) ──

    async def _send_realtime(self):
        await audio_pipeline.send_realtime(self)

    async def _listen_audio(self):
        await audio_pipeline.listen_audio(self)

    async def _run_fast_path(self) -> None:
        await fast_path.run_fast_path(self)

    async def _handle_fast_path_utterance(self, audio: np.ndarray) -> None:
        await fast_path.handle_fast_path_utterance(self, audio)

    async def _handle_fast_path_utterance_inner(self, audio: np.ndarray) -> None:
        await fast_path.handle_fast_path_utterance_inner(self, audio)

    async def _receive_audio(self):
        await audio_pipeline.receive_audio(self)

    async def _play_audio(self):
        await audio_pipeline.play_audio(self)

    # ── System monitor ──────────────────────────────────────────────────────────

    async def _run_system_monitor(self) -> None:
        """Background task: voice alerts when metrics exceed thresholds."""
        while True:
            await asyncio.sleep(10)
            alert = await asyncio.to_thread(self._sys_monitor.check)
            if alert and self.session:
                try:
                    await self.session.send_client_content(
                        turns={"parts": [{"text": alert}]},
                        turn_complete=True,
                    )
                except Exception as e:
                    print(f"[Monitor] ⚠️ Could not send alert: {e}")

    # ── Proactive mode ──────────────────────────────────────────────────────────

    async def _run_proactive_mode(self) -> None:
        """
        Background task: periodically checks if the user has been silent long enough,
        then hands time + memory context to Gemini so it can decide what (if anything)
        to say proactively. No hardcoded rules — Gemini makes the call.
        """
        while True:
            await asyncio.sleep(60)   # evaluate once per minute

            if not self.session:
                continue

            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue

            if not self._proactive.should_trigger(self._last_user_speech):
                continue

            self._proactive.mark_triggered()

            try:
                memory = await asyncio.to_thread(load_memory)
                prompt = self._proactive.build_prompt(memory)
                await self.session.send_client_content(
                    turns={"parts": [{"text": prompt}]},
                    turn_complete=True,
                )
                self.ui.write_log("SYS: Proactive check-in.")
            except Exception as e:
                print(f"[Proactive] ⚠️ {e}")

    # ── Phone audio relay ────────────────────────────────────────────────────────

    async def _relay_phone_audio(self) -> None:
        """Forward phone mic PCM chunks from dashboard queue into the Gemini Live session."""
        q = self._dashboard._phone_audio_queue
        while True:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # No audio for 1 s → phone mic inactive, give PC mic back
                self._phone_active = False
                continue
            self._phone_active = True   # phone is streaming — silence PC mic
            with self._speaking_lock:
                speaking = self._is_speaking
            if not speaking and not self.ui.muted:
                try:
                    self.out_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass

    def _on_phone_connected(self) -> None:
        self.ui.write_log("SYS: Phone connected via Remote Dashboard.")
        self.ui.notify_phone_connected()

    # ── dashboard command relay ─────────────────────────────────────────────

    async def _process_dashboard_commands(self) -> None:
        while True:
            try:
                text = await asyncio.wait_for(
                    self._dashboard._command_queue.get(), timeout=0.5
                )
                if not text:
                    continue
                # Wait up to 8s for session to become ready after a wake
                for _ in range(80):
                    if self.session:
                        break
                    await asyncio.sleep(0.1)
                if self.session:
                    await self.session.send_client_content(
                        turns={"parts": [{"text": text}]},
                        turn_complete=True,
                    )
                    self.ui.write_log(f"[Web]: {text}")
                else:
                    print(f"[Dashboard] Dropped command (no session): {text}")
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                print(f"[Dashboard] Command error: {e}")
                await asyncio.sleep(0.5)

    # ── Telegram userbot command relay ──────────────────────────────────────

    async def _process_telegram_commands(self) -> None:
        if not self._telegram:
            return
        while True:
            try:
                text, chat_id = await asyncio.wait_for(
                    self._telegram._command_queue.get(), timeout=0.5
                )
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"[Telegram] Command error: {e}")
                await asyncio.sleep(0.5)
                continue

            if not text:
                continue
            try:
                # Wait up to 8s for session to become ready after a wake
                for _ in range(80):
                    if self.session:
                        break
                    await asyncio.sleep(0.1)
                if self.session:
                    self._telegram_reply_target = chat_id
                    self._telegram_audio_chunks = []
                    await self.session.send_client_content(
                        turns={"parts": [{"text": text}]},
                        turn_complete=True,
                    )
                    self.ui.write_log(f"[Telegram]: {text}")
                else:
                    print(f"[Telegram] Dropped command (no session): {text}")
            except Exception as e:
                # Unlike the queue.get() try/except above, this used to be
                # unguarded — a single send_client_content() failure (e.g.
                # session torn down mid-send during a reconnect) killed this
                # whole background task permanently, silently dropping every
                # Telegram command for the rest of the process's life.
                # _process_dashboard_commands() already guards its equivalent
                # send; mirror that here so one bad send can't end the relay.
                print(f"[Telegram] Failed to relay command to Gemini: {e}")

    # ── main loop ───────────────────────────────────────────────────────────

    async def run(self):
        self._loop = asyncio.get_event_loop()

        # Application Registry — index every Start Menu shortcut once, in the
        # background, so the FIRST "open Discord" of the session doesn't pay
        # a live filesystem scan either (only repeat launches were cached
        # before this). Non-blocking: open_app.py falls back to a live scan
        # until this finishes, so nothing is broken while it's still running.
        def _build_app_index():
            try:
                from actions.open_app import build_app_index
                n = build_app_index()
                print(f"[AppRegistry] Indexed {n} Start Menu shortcuts.")
            except Exception as e:
                print(f"[AppRegistry] Index build failed (falling back to live scans): {e}")
        asyncio.create_task(asyncio.to_thread(_build_app_index))

        # Start dashboard (optional — needs: pip install fastapi "uvicorn[standard]" cryptography)
        try:
            from dashboard.server import DashboardServer
            self._dashboard = DashboardServer()
            self._dashboard.set_connect_callback(self._on_phone_connected)
            asyncio.create_task(self._dashboard.serve())
            # Runs for the whole lifetime, not just inside an active session
            asyncio.create_task(self._process_dashboard_commands())
        except Exception as e:
            print(f"[Dashboard] Disabled: {e}")
            self._dashboard = None

        # Telegram userbot (second account) — optional, needs: pip install telethon
        # and a prior one-time login via integrations/telegram_login.py
        try:
            from integrations.telegram_userbot import TelegramUserbot
            self._telegram = TelegramUserbot()
            asyncio.create_task(self._telegram.run())
            asyncio.create_task(self._process_telegram_commands())
        except Exception as e:
            print(f"[Telegram] Disabled: {e}")
            self._telegram = None

        # Windows Control Layer self-check — quick, non-blocking sanity check
        # of native Win32/UI Automation/keyboard-mouse/screenshot/process
        # subsystems, so a broken component shows up in the log immediately
        # instead of as a confusing tool failure three commands later.
        #
        # Also pre-warms UI Automation's COM/IUIAutomation interface here
        # (measured one-time cost: ~6ms on the first real call in a process,
        # ~0.6ms every call after) so it's already paid for by the time the
        # user's first "click X" / "focus Y" command needs it, instead of
        # that command eating the extra latency.
        diag: dict = {}
        try:
            from windows_control.diagnostics import run_self_check, summarize
            from windows_control.uia import prewarm as _prewarm_uia
            diag = await asyncio.to_thread(run_self_check)
            await asyncio.to_thread(_prewarm_uia)
            self.ui.write_log(f"SYS: {summarize(diag)}")
        except Exception as e:
            print(f"[WinControl] Self-check skipped: {e}")

        # Fast Path model loading (wake word, VAD, STT, TTS — ~7s) runs as a
        # background task, NOT awaited here, so it never delays connecting to
        # Gemini / starting to listen. _run_fast_path()'s wake-word check
        # already no-ops until self._voice_pipeline.is_ready flips true.
        def _prewarm_voice_pipeline():
            try:
                self._voice_pipeline.prewarm()
                msg = "SYS: Fast Path ready (local wake word / STT / TTS)."
                self.ui.write_log(msg)
                print(f"[FastPath] {msg}")  # ui.write_log() only reaches the UI panel, never stdout
            except Exception as e:
                print(f"[FastPath] Prewarm failed — Fast Path disabled this session: {e}")
            try:
                self._intent_classifier.load()
                msg = "SYS: Fast Path fuzzy-intent tier ready (MiniLM)."
                self.ui.write_log(msg)
                print(f"[FastPath] {msg}")
            except Exception as e:
                print(f"[FastPath] MiniLM classifier load failed — fuzzy tier disabled this session: {e}")

            # Push real subsystem status to the HUD's constellation view
            # (each node reflects an actual component, not a decoration —
            # see HudCanvas._NODES in ui.py). windows_control status folds
            # every self-check result into one online/offline flag.
            windows_ok = bool(diag) and all(ok for ok, _ in diag.values())
            self.ui.set_subsystem_status({
                "fast_path":  self._voice_pipeline.is_ready,
                "minilm":     self._intent_classifier.is_ready,
                "smart_path": True,
                "windows":    windows_ok,
                "web_search": bool(_get_api_key()) or bool(runtime_config.get_claude_api_key()),
                "memory":     True,
                "telegram":   self._telegram is not None,
                "dashboard":  self._dashboard is not None,
            })
        asyncio.create_task(asyncio.to_thread(_prewarm_voice_pipeline))

        while True:
            try:
                print("[JARVIS] Connecting...")
                self.ui.set_state("THINKING")
                config = self._build_config()

                # Fresh client on every reconnect — avoids stale HTTP session state
                client = genai.Client(
                    api_key=_get_api_key(),
                    http_options={"api_version": "v1beta"}
                )

                async with (
                    client.aio.live.connect(model=LIVE_MODEL, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session          = session
                    self.audio_in_queue   = asyncio.Queue()
                    self.out_queue        = asyncio.Queue(maxsize=200)
                    self._turn_done_event = asyncio.Event()

                    # Reset transient state that must not carry over from a previous session
                    self._pending_vision       = None
                    self._vision_cam_active    = False
                    self._vision_close_pending = False
                    self._vision_busy          = False
                    self._vision_last_time     = 0.0
                    self._interrupted          = False
                    with self._fast_path_lock:
                        self._fast_path_state = "idle"
                    self._fast_path_queue = None  # _run_fast_path() recreates it on (re)start

                    print("[JARVIS] Connected.")
                    self.ui.set_state("LISTENING")
                    self.ui.write_log("SYS: JARVIS online.")

                    if self._dashboard:
                        await self._dashboard.broadcast({"type": "status", "state": "active"})

                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self._run_fast_path())
                    tg.create_task(self._run_system_monitor())
                    tg.create_task(self._run_proactive_mode())
                    if self._dashboard:
                        tg.create_task(self._relay_phone_audio())

            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except BaseException as e:
                # Catches both Exception and BaseExceptionGroup (Python 3.11+
                # TaskGroup raises BaseExceptionGroup when tasks are cancelled
                # externally, which `except Exception` would miss, letting the
                # exception escape the while-loop and causing asyncio.run() to
                # start shutdown — resulting in "executor after shutdown" errors).
                print(f"[JARVIS] Error ({type(e).__name__}): {e}")
                traceback.print_exc()

                # `e` itself is usually just an (Base)ExceptionGroup wrapper
                # once we're inside the TaskGroup — str(e) is always the
                # generic "unhandled errors in a TaskGroup (N sub-exceptions)"
                # and never contains the real error, so every keyword check
                # below used to silently never match for in-session errors
                # (network drops, Gemini 1011s, PortAudioError, ...) and they
                # all fell into the catch-all `else` branch below. Unwrap to
                # the real leaf exception(s) so classification actually works.
                leaves = _flatten_exceptions(e)
                err_str = "; ".join(f"{type(x).__name__}: {x}" for x in leaves)

                # Local audio device problem (no driver / device unplugged) —
                # this has nothing to do with the Gemini connection, so
                # reconnecting rapidly every few seconds just spams retries
                # that can never succeed until the user fixes their sound
                # settings. Surface it plainly and back off slowly instead.
                if any(_is_audio_device_error(x) for x in leaves):
                    self.ui.write_log(
                        "AUDIO: не найдено аудиоустройство (нет драйвера или устройство "
                        "недоступно) — проверьте настройки звука Windows. "
                        "Переподключение к Gemini это не исправит."
                    )
                    # Falls through to the shared reconnect-delay tail below
                    # (same as every other branch) — do NOT `continue` here,
                    # that would skip the `await asyncio.sleep(delay)` at the
                    # bottom of the loop and spin-reconnect with no wait at all.
                    self._conn_backoff = 20

                # Invalid API key — stop hammering the API, prompt re-configuration
                elif "API key not valid" in err_str or "1007" in err_str:
                    self.ui.write_log("ERR: API key invalid — please re-enter your key.")
                    self.ui.set_state("SLEEPING")
                    self.ui.prompt_reconfig()
                    while not self.ui._win._ready:
                        await asyncio.sleep(1)
                    print("[JARVIS] New API key saved — reconnecting...")
                    _conn_backoff = 3
                    continue

                # Network / timeout errors — log clearly and back off
                elif any(k in err_str for k in (
                    "TimeoutError", "timed out", "getaddrinfo", "CancelledError",
                    "ConnectionRefusedError", "OSError", "Cannot connect",
                )):
                    _conn_backoff = min(getattr(self, "_conn_backoff", 3) * 2, 60)
                    self._conn_backoff = _conn_backoff
                    self.ui.write_log(
                        f"NET: Bağlantı kurulamadı — {_conn_backoff}s sonra tekrar deneniyor. "
                        "(VPN gerekiyor olabilir)"
                    )
                else:
                    self._conn_backoff = 3
            finally:
                self.session = None

            self.set_speaking(False)
            self.ui.set_state("SLEEPING")

            if self._dashboard:
                await self._dashboard.broadcast({"type": "status", "state": "sleeping"})

            delay = getattr(self, "_conn_backoff", 3)
            print(f"[JARVIS] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)

def main():
    ui = JarvisUI("face.png")

    def runner():
        ui.wait_for_api_key()
        jarvis = JarvisLive(ui)
        try:
            asyncio.run(jarvis.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()

if __name__ == "__main__":
    main()