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
from memory.memory_manager import load_memory, format_memory_for_prompt
from core.assistant_state import get_state
from core.config import GEMINI_LIVE_MODEL as LIVE_MODEL, GEMINI_VOICE_NAME

from actions.system_monitor    import SystemMonitor
from actions.proactive         import ProactiveEngine

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
# Audio pipeline (mic/speaker I/O, response handling), Fast Path (local
# wake word/STT/router/TTS), and the tool dispatcher -- moved to
# core/audio_pipeline.py, core/fast_path.py and core/tool_dispatch.py in
# the Stage 2 module split (see REWORK_PLAN.md). JarvisLive keeps thin
# `_foo` wrapper methods that delegate into these.
from core import audio_pipeline, fast_path, tool_dispatch, tool_registry
from core.hotkey import GlobalHotkey
from core import local_llm
from core import routine_bridge
from core import system_monitor_bridge, dashboard_bridge, telegram_bridge
from core.audio_pipeline import CHANNELS, SEND_SAMPLE_RATE, RECEIVE_SAMPLE_RATE, CHUNK_SIZE
from core.idle_watchdog import run_idle_watchdog, IntentionalIdleClose


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
        "name": "system_scan",
        "description": (
            "Runs a real Microsoft Defender antivirus scan. Use when the user says "
            "scan the system, check for viruses, scan for malware, or asks whether "
            "their computer is infected. Default to a quick scan; use scan_type='full' "
            "only when the user explicitly requests a complete/full scan."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "scan_type": {"type": "STRING", "description": "quick (default) | full"}
            },
            "required": []
        }
    },
    {
        "name": "system_diagnostics",
        "description": "Checks local computer health: CPU, RAM, GPU, disk space, uptime and processes. Use when the user asks to check, diagnose or optimize their computer.",
        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
    },
    {
        "name": "process_hunter",
        "description": (
            "Scans all running processes for suspicious activity: unsigned "
            "executables, processes running from Temp/Downloads, unusually high "
            "CPU or RAM use, and open network connections. Use when the user asks "
            "to find suspicious/malicious processes, check what's running, hunt "
            "for malware, or investigate resource hogs. Read-only — reports "
            "findings, does not kill or modify anything."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "limit": {"type": "NUMBER", "description": "Max number of findings to report (default 10)"}
            },
            "required": []
        }
    },
    {
        "name": "hacker_terminal",
        "description": (
            "Executes an arbitrary PowerShell or CMD command on the user's computer "
            "and returns its output. This is unrestricted — there is no command "
            "whitelist — and runs immediately, no confirmation needed."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "command":     {"type": "STRING", "description": "The exact PowerShell or CMD command to run"},
                "shell":       {"type": "STRING", "description": "powershell (default) or cmd"},
                "working_dir": {"type": "STRING", "description": "Optional working directory (default: user's home folder)"},
                "timeout":     {"type": "NUMBER", "description": "Optional timeout in seconds (default 30, max 120)"},
            },
            "required": ["command"]
        }
    },
    {
        "name": "digital_ghost",
        "description": (
            "Queries JARVIS's background system baseline (Digital Ghost): a "
            "continuously updated snapshot of processes, files in watched "
            "folders, startup/persistence entries, services, network "
            "destinations, DNS lookups, USB/PnP devices, installed "
            "applications, and system event log entries. Use action='delta' "
            "when the user asks what changed on their PC recently (e.g. "
            "'what changed in the last 6 hours', 'what's new since this "
            "morning', 'did anything change after I installed X') — set "
            "'hours' to match what they asked for. Use action='status' when "
            "they ask whether Ghost/the baseline monitor is running or how "
            "much it's tracking. Read-only, makes no changes."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "delta (default) | status"},
                "hours":  {"type": "NUMBER", "description": "For action=delta: how many hours back to compare (default 6)"},
            },
            "required": []
        }
    },
    {
        "name": "jarvis_control",
        "description": "Manages JARVIS tasks, privacy permissions, focus mode, response style, saved routines and activity history. Use it whenever the user asks to create/list/complete/cancel a task, change a privacy permission, enable/disable focus mode, change response detail, save/run/list/delete a routine, or view activity.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "status | task_list | task_add | task_complete | task_cancel | privacy_get | privacy_set | focus_on | focus_off | focus_status | voice_set | qwen_on | qwen_off | qwen_status | power_on | power_off | power_status | routine_list | routine_save | routine_run | routine_schedule | routine_delete | activity"},
                "value": {"type": "STRING", "description": "Task title, privacy permission, focus end time, voice style (brief|balanced|detailed), or routine instruction."},
                "id": {"type": "STRING", "description": "Task or routine id for complete, cancel, run or delete."},
                "name": {"type": "STRING", "description": "Routine name when saving a routine."},
                "enabled": {"type": "BOOLEAN", "description": "Allow or block a privacy permission."}
            },
            "required": ["action"]
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
            "or getting video info. Trending videos are NOT currently available -- if the "
            "user asks for trending/popular videos, tell them that's not working right now "
            "instead of calling this tool with action=trending."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "play | summarize | get_info (default: play)"},
                "query":  {"type": "STRING", "description": "Search query for play action"},
                "save":   {"type": "BOOLEAN", "description": "Save summary to Notepad (summarize only)"},
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
            "Use for ANY single computer control command. Every action runs immediately with no "
            "confirmation EXCEPT shutdown, which is gated -- your first shutdown call returns a "
            "confirmation prompt instead of running; only call it again with confirmed=true after "
            "the user clearly says yes."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": (
                        "One of: brightness_down | brightness_up | close_app | close_tab | "
                        "close_window | copy | cut | dark_mode | enter | escape | file_explorer | "
                        "find_on_page | focus_search | full_screen | fullscreen | go_back | "
                        "go_forward | lock_screen | maximize | minimize | mute | new_tab | "
                        "next_tab | open_run | open_settings | page_down | page_up | paste | "
                        "pause_video | play_pause | press_key | prev_tab | redo | refresh_page | "
                        "reload | reload_n | restart | save | screen_off | screenshot | "
                        "scroll_bottom | scroll_down | scroll_top | scroll_up | select_all | "
                        "show_desktop | shutdown | sleep_display | snap_left | snap_right | "
                        "switch_window | task_manager | toggle_mute | toggle_wifi | type_text | "
                        "undo | unmute | volume_down | volume_set | volume_up | zoom_in | "
                        "zoom_out | zoom_reset. If truly unsure which one applies, omit 'action' "
                        "and pass 'description' instead — a fuzzy matcher picks the closest one."
                    ),
                },
                "description": {"type": "STRING", "description": "Natural language description of what to do — used to pick 'action' when you didn't pass one, or to correct it if it doesn't match the list above"},
                "value":       {"type": "STRING", "description": "Optional value: volume level (0-100 for volume_set), text to type (type_text), key name (press_key), reload count (reload_n)"}
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
        "name": "computer_agent",
        "description": (
            "Autonomous agent that carries out a WHOLE multi-step goal on the computer by "
            "looking at the screen and clicking/typing until it is done: registering on a "
            "website, filling in forms, configuring an app, navigating settings, working "
            "through a slow game menu. Use it when the task needs several UI steps you "
            "can't do with one call. For a single click/keypress use computer_control. "
            "It cannot pass CAPTCHAs or SMS/e-mail codes: if the result starts with "
            "[NEEDS_USER], tell the user what they must do on screen, and after they are "
            "done call computer_agent again with the same goal. action=stop aborts a run."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "goal":        {"type": "STRING",  "description": "What to achieve, in plain language, with every detail the agent needs (site, names, options)."},
                "max_steps":   {"type": "INTEGER", "description": "Action budget (default 25, max 60)"},
                "max_seconds": {"type": "NUMBER",  "description": "Time budget in seconds (default 300)"},
                "action":      {"type": "STRING",  "description": "Set to 'stop' to abort a running task. Omit to start one."},
            },
            "required": []
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
    {
        "name": "add_capability",
        "description": (
            "Writes and adds a brand-new capability to JARVIS itself, using Claude, based on what the "
            "user describes wanting (e.g. 'add Spotify control', 'make yourself able to track a stock price'). "
            "Runs immediately, no confirmation needed. The new capability becomes usable starting next time, "
            "not this turn -- mention that in plain, conversational terms, don't list code or permissions formally."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "description": {"type": "STRING", "description": "Plain description of the capability to add, in the user's own words"},
            },
            "required": ["description"]
        }
    },
]

# Retain the optional `confirmed` schema field for compatibility with older
# Gemini prompts; this installation does not require confirmations.
tool_registry.add_confirmation_param(TOOL_DECLARATIONS)

# --- Plugin system ---
# Self-authored capabilities (actions/self_extend.py's add_capability tool)
# from a previous session -- reload them now so they're usable again
# without regenerating, before the first Gemini connect ever reads
# TOOL_DECLARATIONS. Read-only + in-memory registration, safe at import
# time (see core/custom_tools.py); one bad file is skipped, not fatal.
try:
    from core import custom_tools as _custom_tools
    _custom_tools.set_tool_declarations(TOOL_DECLARATIONS)
    _custom_tools.reload_persisted_tools()
except Exception as e:
    print(f"[CustomTools] Failed to reload self-authored tools: {e}")


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
        # Round-trip latency instrumentation (see core/audio_pipeline.py's
        # _mark_response_started) -- core/latency.py only ever measured tool
        # execution time; these two flags let it also measure the one leg
        # that's otherwise invisible: Gemini's own STT+reasoning+TTS-start
        # time, both for a plain conversational turn and for its reaction
        # after a tool's function_response comes back.
        self._turn_measured          = True   # False from new user speech until the model's first reply signal
        self._tool_reaction_measured = True   # False from send_tool_response() until the model's first reply signal
        self._tool_response_sent_at: float | None = None
        self._audio_response_received_at: float | None = None
        self._speaker_start_pending = False
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

        # Wake-word gate — the app starts silent (no Gemini connection, no
        # HUD) with only the local wake-word detector running; the first
        # utterance that reaches _on_text_command while gated becomes the
        # opening line of a new session. Cleared once connected, re-armed
        # by core/idle_watchdog.py after a period of silence. See
        # REWORK_PLAN.md-style module docs in core/idle_watchdog.py.
        self._require_wake_word: bool = True
        self._wake_event = asyncio.Event()
        self._pending_wake_text: str | None = None

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
        self._hotkey: GlobalHotkey | None = None

    def _on_system_scan_status(self, stage: str, message: str) -> None:
        if stage in {"COMPLETE", "THREATS", "ERROR"}:
            self.speak(message)

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

    def _trigger_wake(self, text: str) -> bool:
        """Wake run()'s connect-gate with `text` as the opening line, if
        currently gated; no-op otherwise. Loop-thread only -- off-loop
        callers (e.g. _on_text_command, invoked from Qt/worker threads via
        main_window.py's bare threading.Thread(...) calls) must go through
        self._loop.call_soon_threadsafe(self._trigger_wake, text) instead
        of calling this directly.

        Returns True if `text` was consumed as the new session's opening
        line (run() will send it once connected) -- callers that would
        otherwise separately send `text` themselves once a session exists
        (dashboard_bridge.py, telegram_bridge.py) MUST skip that send when
        this returns True, or the opener gets sent twice."""
        if self._require_wake_word and not self.session:
            self._pending_wake_text = text
            self._wake_event.set()
            return True
        return False

    def _on_text_command(self, text: str):
        if not self._loop:
            return
        if not self.session:
            if self._require_wake_word:
                self._loop.call_soon_threadsafe(self._trigger_wake, text)
                return
            asyncio.run_coroutine_threadsafe(self._handle_offline_text_command(text), self._loop)
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    async def _handle_offline_text_command(self, text: str) -> None:
        handled = await fast_path.handle_local_text(self, text)
        if not handled:
            local = get_state()["local_llm"]
            message = None
            if local["enabled"]:
                message = await asyncio.to_thread(local_llm.answer, text, 20.0, local["model"])
            if not message:
                message = "Cloud connection is unavailable. I can still handle supported local commands. Enable Qwen offline fallback if you want local conversation."
            self.ui.write_log(f"Jarvis: {message}")
            if self._voice_pipeline.is_ready:
                self.set_speaking(True)
                try:
                    await asyncio.to_thread(self._voice_pipeline.tts.speak, message)
                finally:
                    self.set_speaking(False)

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
        preferences = get_state()

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
        parts.append(
            f"[JARVIS PREFERENCES]\nResponse style: {preferences['voice']['reply_length']}. "
            f"Focus mode: {'active' if preferences['focus']['active'] else 'off'}.\n"
        )
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
            # Both left at SDK defaults until now -- same class of fix already
            # measured for web_search (core/config.py's CLAUDE_FAST_MODEL):
            # less "thinking" before responding, and less silence required
            # before the server decides the user is done talking. Unlike
            # web_search this can't be measured against a live mic in this
            # environment -- only that the session still connects cleanly
            # with these set. Watch for: (a) worse tool-call accuracy on
            # ambiguous requests from thinking_budget=0, (b) JARVIS cutting
            # in before you've finished a sentence from a too-low
            # silence_duration_ms. Revert either independently if so.
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    silence_duration_ms=400,
                )
            ),
        )

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        return await tool_dispatch.execute_tool(self, fc)

    # ── Audio pipeline + Fast Path + tool dispatch (moved to
    # core/audio_pipeline.py, core/fast_path.py and core/tool_dispatch.py
    # in the Stage 2 module split, see REWORK_PLAN.md) ──

    async def _send_realtime(self):
        await audio_pipeline.send_realtime(self)

    async def _listen_audio(self):
        await audio_pipeline.listen_audio(self)

    async def _run_fast_path(self) -> None:
        await fast_path.run_fast_path(self)

    async def _handle_fast_path_wake_utterance(self, audio: np.ndarray) -> None:
        await fast_path.handle_fast_path_wake_utterance(self, audio)

    async def _handle_fast_path_utterance(self, audio: np.ndarray) -> None:
        await fast_path.handle_fast_path_utterance(self, audio)

    async def _handle_fast_path_utterance_inner(self, audio: np.ndarray) -> None:
        await fast_path.handle_fast_path_utterance_inner(self, audio)

    def _on_hotkey_pressed(self) -> None:
        """Fires on the hotkey's own background thread (core/hotkey.py) --
        never call fast_path.trigger_manual_listen() directly from here,
        hop onto the asyncio loop first, same as _trigger_wake()."""
        print("[Hotkey] F10 pressed")  # confirms the OS delivered the key at all — check this first when debugging
        if self._loop:
            self._loop.call_soon_threadsafe(fast_path.trigger_manual_listen, self)

    async def _receive_audio(self):
        await audio_pipeline.receive_audio(self)

    async def _play_audio(self):
        await audio_pipeline.play_audio(self)

    # ── System monitor ──────────────────────────────────────────────────────────

    # ── System monitor / Proactive mode / Dashboard bridge / Telegram bridge
    # (moved to core/system_monitor_bridge.py, core/dashboard_bridge.py and
    # core/telegram_bridge.py in the Stage 2 module split, see
    # REWORK_PLAN.md) ──

    async def _run_system_monitor(self) -> None:
        await system_monitor_bridge.run_system_monitor(self)

    async def _run_proactive_mode(self) -> None:
        await system_monitor_bridge.run_proactive_mode(self)

    async def _relay_phone_audio(self) -> None:
        await dashboard_bridge.relay_phone_audio(self)

    def _on_phone_connected(self) -> None:
        dashboard_bridge.on_phone_connected(self)

    async def _process_dashboard_commands(self) -> None:
        await dashboard_bridge.process_dashboard_commands(self)

    async def _process_telegram_commands(self) -> None:
        await telegram_bridge.process_telegram_commands(self)

    # ── main loop ───────────────────────────────────────────────────────────

    async def run(self):
        self._loop = asyncio.get_event_loop()
        self.ui.set_power_mode(get_state()["power_mode"]["enabled"])

        # The local wake-word/STT/router path remains usable while Gemini is
        # reconnecting or has not been configured yet.
        asyncio.create_task(self._listen_audio())
        asyncio.create_task(self._run_fast_path())

        # Global push-to-talk hotkey (F10) — lets the user start Fast Path
        # listening with a key press instead of saying "джарвис".
        # See core/hotkey.py for why this doesn't need Gemini/a session.
        try:
            self._hotkey = GlobalHotkey(self._on_hotkey_pressed)
            if self._hotkey.start():
                msg = "SYS: Hotkey ready — press F10 to talk to Jarvis."
                self.ui.write_log(msg)
                print(f"[Hotkey] {msg}")  # ui.write_log() only reaches the UI panel, never stdout
            else:
                msg = "SYS: Hotkey unavailable (F10 already claimed by another app, or not on Windows)."
                self.ui.write_log(msg)
                print(f"[Hotkey] {msg}")
        except Exception as e:
            print(f"[Hotkey] Disabled: {e}")

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

        # Digital Ghost — background system-state baseline (processes,
        # files, registry/persistence, services, network, DNS, devices,
        # applications, event log). Runs on its own daemon thread (see
        # ghost/engine.py), first baseline scan takes a few seconds; the
        # digital_ghost tool self-reports "still building" until it's done
        # rather than the user hitting a confusing empty result.
        try:
            from ghost.engine import ghost_engine
            ghost_engine.start()
            self.ui.write_log("SYS: Digital Ghost baseline monitor started.")
        except Exception as e:
            print(f"[Ghost] Disabled: {e}")

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

        # Fast Path model loading (wake word, VAD, STT, TTS — voice pipeline
        # — and separately, MiniLM for the fuzzy-intent tier) runs as
        # background tasks, NOT awaited here, so it never delays connecting
        # to Gemini / starting to listen. _run_fast_path()'s wake-word check
        # already no-ops until self._voice_pipeline.is_ready flips true.
        #
        # The two loads are independent (different libraries, no shared
        # state) but used to run sequentially inside one thread -- measured
        # via core/latency.py as roughly 5s + 5s back to back
        # (voice_pipeline_prewarm, intent_classifier_load), meaning voice
        # wake-word detection wasn't available for ~10s after launch.
        # Running them concurrently on two threads instead cuts that to
        # ~5s (the slower of the two) -- this now directly shortens how
        # long "джарвис" doesn't work for right after startup, given
        # Fast Path's wake-word gate is what wakes main.py's run() (see
        # _require_wake_word).
        def _load_voice_pipeline():
            try:
                self._voice_pipeline.prewarm()
                msg = "SYS: Fast Path ready (local wake word / STT / TTS)."
                self.ui.write_log(msg)
                print(f"[FastPath] {msg}")  # ui.write_log() only reaches the UI panel, never stdout
            except Exception as e:
                print(f"[FastPath] Prewarm failed — Fast Path disabled this session: {e}")

        def _load_intent_classifier():
            try:
                self._intent_classifier.load()
                msg = "SYS: Fast Path fuzzy-intent tier ready (MiniLM)."
                self.ui.write_log(msg)
                print(f"[FastPath] {msg}")
            except Exception as e:
                print(f"[FastPath] MiniLM classifier load failed — fuzzy tier disabled this session: {e}")

        async def _prewarm_fast_path():
            await asyncio.gather(
                asyncio.to_thread(_load_voice_pipeline),
                asyncio.to_thread(_load_intent_classifier),
            )
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
        asyncio.create_task(_prewarm_fast_path())

        while True:
            try:
                if not _get_api_key():
                    self.ui.write_log("SYS: Cloud mode is unavailable until a Gemini API key is configured.")
                    self.ui.set_state("LISTENING")
                    await asyncio.sleep(5)
                    continue

                if self._require_wake_word:
                    # Silent/ambient state: no Gemini connection, no compact
                    # bar, just the local wake-word detector (voice/wake_word.py
                    # via core/fast_path.py) running. _trigger_wake() -- reached
                    # from voice, typed, dashboard, or Telegram input while
                    # gated -- sets _wake_event with _pending_wake_text as the
                    # opening line.
                    self.ui.set_state("SLEEPING")
                    await self._wake_event.wait()
                    self._wake_event.clear()

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

                    # core/fast_path.py's wake-word branch already shows this
                    # immediately on detection (before transcription) for
                    # voice wakes -- this covers every OTHER path that can
                    # reach a connection (typed text, dashboard, Telegram,
                    # and any ordinary error-recovery reconnect of an
                    # already-active conversation). Idempotent if already
                    # shown.
                    self.ui.show_compact_bar()

                    # Reset transient state that must not carry over from a previous session
                    self._pending_vision       = None
                    self._vision_cam_active    = False
                    self._vision_close_pending = False
                    self._vision_busy          = False
                    self._vision_last_time     = 0.0
                    self._interrupted          = False
                    self._turn_measured          = True
                    self._tool_reaction_measured = True
                    self._tool_response_sent_at  = None
                    self._audio_response_received_at = None
                    self._speaker_start_pending = False
                    with self._fast_path_lock:
                        self._fast_path_state = "idle"

                    print("[JARVIS] Connected.")
                    self.ui.set_state("LISTENING")
                    self.ui.write_log("SYS: JARVIS online.")

                    if self._dashboard:
                        await self._dashboard.broadcast({"type": "status", "state": "active"})

                    # Wake word (or typed/dashboard/Telegram text) reached us
                    # while gated -- that utterance is the opening line.
                    if self._pending_wake_text:
                        opener, self._pending_wake_text = self._pending_wake_text, None
                        await session.send_client_content(
                            turns={"parts": [{"text": opener}]},
                            turn_complete=True,
                        )
                    self._require_wake_word = False

                    tg.create_task(self._send_realtime())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self._run_system_monitor())
                    tg.create_task(self._run_proactive_mode())
                    tg.create_task(routine_bridge.run_scheduled_routines(self))
                    tg.create_task(run_idle_watchdog(self))
                    if self._dashboard:
                        tg.create_task(self._relay_phone_audio())

            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except BaseException as e:
                # `e` itself is usually just an (Base)ExceptionGroup wrapper
                # once we're inside the TaskGroup — str(e) is always the
                # generic "unhandled errors in a TaskGroup (N sub-exceptions)"
                # and never contains the real error, so every keyword check
                # below used to silently never match for in-session errors
                # (network drops, Gemini 1011s, PortAudioError, ...) and they
                # all fell into the catch-all `else` branch below. Unwrap to
                # the real leaf exception(s) so classification actually works.
                leaves = _flatten_exceptions(e)
                is_idle_close = any(isinstance(x, IntentionalIdleClose) for x in leaves)

                # Catches both Exception and BaseExceptionGroup (Python 3.11+
                # TaskGroup raises BaseExceptionGroup when tasks are cancelled
                # externally, which `except Exception` would miss, letting the
                # exception escape the while-loop and causing asyncio.run() to
                # start shutdown — resulting in "executor after shutdown" errors).
                # Skip the noisy error print/traceback for a deliberate
                # idle-close -- it isn't an error, just going quiet on purpose.
                if not is_idle_close:
                    print(f"[JARVIS] Error ({type(e).__name__}): {e}")
                    traceback.print_exc()
                err_str = "; ".join(f"{type(x).__name__}: {x}" for x in leaves)

                # Deliberate idle-close (core/idle_watchdog.py) -- not an
                # error, so it must be classified before every branch below:
                # no error log, no backoff, just re-arm the wake gate so the
                # next connection waits for the wake word again.
                if is_idle_close:
                    self._require_wake_word = True
                    self.ui.hide_compact_bar()

                # Local audio device problem (no driver / device unplugged) —
                # this has nothing to do with the Gemini connection, so
                # reconnecting rapidly every few seconds just spams retries
                # that can never succeed until the user fixes their sound
                # settings. Surface it plainly and back off slowly instead.
                elif any(_is_audio_device_error(x) for x in leaves):
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
                    # Catch-all -- most commonly a Gemini Live server-side error
                    # (e.g. websocket close code 1011 "Internal error occurred",
                    # seen in jarvis_hud.err.log from an earlier session) that
                    # isn't the user's fault and isn't classifiable as audio/
                    # API-key/network. This branch used to set a 3s backoff with
                    # no ui.write_log() call at all -- every OTHER branch tells
                    # the user something happened, this one reconnected in
                    # silence, which reads as JARVIS randomly going quiet for a
                    # few seconds with no explanation.
                    self._conn_backoff = 3
                    self.ui.write_log(f"ERR: {err_str[:200]} — reconnecting in 3s...")
            finally:
                # Also null the queues, not just the session: the mic
                # callback (core/audio_pipeline.py) only checks
                # `self.out_queue is not None` before enqueuing Gemini-bound
                # audio, so a stale queue left non-None here would keep
                # silently accepting audio nobody drains after any
                # disconnect, until it hits maxsize=200 and raises
                # QueueFull inside the mic callback thread.
                self.session = None
                self.audio_in_queue = None
                self.out_queue = None

            self.set_speaking(False)
            self.ui.set_state("SLEEPING")

            if self._dashboard:
                await self._dashboard.broadcast({"type": "status", "state": "sleeping"})

            delay = getattr(self, "_conn_backoff", 3)
            print(f"[JARVIS] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)

def _handle_startup_command() -> bool:
    commands = {"--install-autostart", "--remove-autostart", "--autostart-status"}
    if not any(command in sys.argv[1:] for command in commands):
        return False
    from core import autostart
    try:
        if "--install-autostart" in sys.argv:
            print(f"Autostart installed: {autostart.install(Path(__file__))}")
        elif "--remove-autostart" in sys.argv:
            print("Autostart removed." if autostart.remove() else "Autostart was not installed.")
        else:
            command = autostart.status()
            print(f"Autostart: {command}" if command else "Autostart is not installed.")
    except OSError as error:
        print(f"Autostart error: {error}")
        return True
    return True


def main():
    if _handle_startup_command():
        return
    ui = JarvisUI("face.png")

    def runner():
        jarvis = JarvisLive(ui)
        try:
            asyncio.run(jarvis.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()

if __name__ == "__main__":
    main()