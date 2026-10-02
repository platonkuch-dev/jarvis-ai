"""The live Claude Code session, shared between processes.

tools/coding_agent.py (in the voice worker) runs Claude Code headless with
stream-json output and writes what it does here, step by step; hud_panel.py
streams the file to the holographic panel, which shows it as a live feed
(what Claude reads, which files it edits, what it says) -- so coding happens
inside Jarvis's own UI instead of a console window. The panel's "Стоп" button
(or "останови код" by voice) sets the stop flag that coding_agent polls.

One tiny JSON file, rewritten atomically: no locks needed between the writer
(one coding session at a time) and the panel's reader.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import config

STATE_FILE = config.DATA_DIR / "code_session.json"
STOP_FILE = config.DATA_DIR / "code_stop.flag"
MAX_EVENTS = 60
MAX_TEXT = 400


def _write(state: dict[str, Any]) -> None:
    state["updated"] = time.time()
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_FILE)


def read() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def mtime() -> float:
    try:
        return STATE_FILE.stat().st_mtime
    except OSError:
        return 0.0


def start(project: str, task: str) -> None:
    STOP_FILE.unlink(missing_ok=True)
    _write({"active": True, "project": project, "task": task[:MAX_TEXT], "started": time.time(),
            "events": [], "result": "", "ok": None, "finished": 0.0})


def add(kind: str, text: str) -> None:
    """kind: "text" (Claude's words), "tool" (an action: read/edit/run), "info"."""
    text = " ".join(str(text).split())
    if not text:
        return
    state = read()
    if not state.get("active"):
        return
    events = state.setdefault("events", [])
    events.append({"t": time.time(), "k": kind, "x": text[:MAX_TEXT]})
    del events[:-MAX_EVENTS]
    _write(state)


def finish(result: str, ok: bool) -> None:
    state = read()
    state.update(active=False, result=" ".join(str(result).split())[:1200], ok=ok, finished=time.time())
    _write(state)
    STOP_FILE.unlink(missing_ok=True)


def request_stop() -> bool:
    if not read().get("active"):
        return False
    STOP_FILE.write_text(str(time.time()), encoding="utf-8")
    return True


def stop_requested() -> bool:
    return STOP_FILE.exists()


def describe_tool(name: str, args: dict[str, Any]) -> str:
    """One readable line for a Claude Code tool call, for the feed."""
    def short(p: Any) -> str:
        return Path(str(p)).name if p else ""

    if name in ("Read", "NotebookRead"):
        return f"Читает {short(args.get('file_path') or args.get('notebook_path'))}"
    if name == "Write":
        return f"Создаёт {short(args.get('file_path'))}"
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return f"Правит {short(args.get('file_path') or args.get('notebook_path'))}"
    if name in ("Bash", "PowerShell"):
        return f"$ {args.get('command', '')}"
    if name in ("Glob", "Grep"):
        return f"Ищет {args.get('pattern', '')}"
    if name in ("WebSearch", "WebFetch"):
        return f"Смотрит в интернете: {args.get('query') or args.get('url') or ''}"
    if name == "TodoWrite":
        todos = args.get("todos") or []
        doing = [t.get("content", "") for t in todos if isinstance(t, dict) and t.get("status") == "in_progress"]
        return f"План: {doing[0]}" if doing else f"План из {len(todos)} шагов"
    if name in ("Task", "Agent"):
        return f"Помощник: {args.get('description', '')}"
    return name
