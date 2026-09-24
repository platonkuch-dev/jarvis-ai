"""Holds references to the live AgentSession and LiveKit room so background
work (timers, reminders, scenario progress) can speak/publish without a
RunContext of its own.

`worker.py` sets them once, right after `session.start()`. Processes without
a voice session (the Telegram bridge, tests) never set them, and every
function here silently no-ops in that case.
"""

from __future__ import annotations

import json
from typing import Any

_active_session: Any | None = None
_active_room: Any | None = None
_active_agent: Any | None = None

SCENARIO_PROGRESS_TOPIC = "lk.scenario_progress"


_presence_probe: Any | None = None


def set_presence_probe(probe: Any) -> None:
    """`probe() -> bool`: True while someone is plausibly at the microphone
    (worker.py wires this to "not asleep")."""
    global _presence_probe
    _presence_probe = probe


def user_present() -> bool:
    """False when nobody is likely to hear a spoken message -- no live voice
    session, or the agent has gone to sleep. Callers use it to also send
    important things to Telegram."""
    if _active_session is None:
        return False
    try:
        return bool(_presence_probe()) if _presence_probe is not None else True
    except Exception:
        return True


def set_active_session(session: Any) -> None:
    global _active_session
    _active_session = session


def get_active_session() -> Any | None:
    return _active_session


def set_active_room(room: Any) -> None:
    global _active_room
    _active_room = room


def get_active_room() -> Any | None:
    return _active_room


def set_active_agent(agent: Any) -> None:
    global _active_agent
    _active_agent = agent


def get_active_agent() -> Any | None:
    return _active_agent


async def say(text: str) -> None:
    """Best-effort spoken announcement; silently no-ops if no session is live."""
    session = get_active_session()
    if session is None:
        return
    handle = session.say(text)
    try:
        await handle.wait_for_playout()
    except Exception:
        pass


async def publish_json(topic: str, payload: dict[str, Any]) -> None:
    """Best-effort structured event over a text data stream (no-ops without a room).

    Uses send_text (Data Streams v2), the same mechanism LiveKit Agents uses
    for transcription -- the older publish_data()/"dataReceived" packet API
    was silently undelivered in testing against this server/client/agents
    version combination, even though the call raised no error locally.
    """
    room = get_active_room()
    if room is None:
        return
    try:
        await room.local_participant.send_text(json.dumps(payload, ensure_ascii=False), topic=topic)
    except Exception:
        pass
