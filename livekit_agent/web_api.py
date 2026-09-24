"""Tiny local API for the web frontend (web/): mints LiveKit join tokens and
exposes read-only scenario/action-log data. Binds to localhost only -- this
is a personal dev tool, not a public service, and holds your LiveKit API
secret in-process.

Run alongside `python worker.py dev` (the frontend needs the worker
registered in room mode, not `console` mode -- see README.md):

    python -m uvicorn web_api:app --host 127.0.0.1 --port 8787
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from livekit.api import AccessToken, VideoGrants

import config

app = FastAPI(title="Jarvis web API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/token")
def get_token(identity: str = "web-user") -> dict[str, str]:
    if not (config.LIVEKIT_URL and config.LIVEKIT_API_KEY and config.LIVEKIT_API_SECRET):
        raise HTTPException(500, "LIVEKIT_URL/LIVEKIT_API_KEY/LIVEKIT_API_SECRET not configured in .env")

    # A fresh room per connection, not a shared fixed name: LiveKit only
    # auto-dispatches an agent job when a room is *created*, not to new
    # participants joining a room that already exists. Reusing one fixed
    # room name means every connection after the first joins a room whose
    # agent session already closed when the previous participant left.
    room_name = f"jarvis-web-{uuid.uuid4().hex[:10]}"

    token = (
        AccessToken(config.LIVEKIT_API_KEY, config.LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(VideoGrants(room_join=True, room=room_name, can_publish=True, can_subscribe=True))
        .with_ttl(timedelta(hours=6))
    )
    return {"token": token.to_jwt(), "url": config.LIVEKIT_URL, "room": room_name, "identity": identity}


@app.get("/api/scenarios")
def get_scenarios() -> dict[str, Any]:
    if not config.SCENARIOS_FILE.exists():
        return {}
    try:
        return json.loads(config.SCENARIOS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


@app.get("/api/actions")
def get_actions(limit: int = 50) -> list[dict[str, Any]]:
    if not config.TOOL_LOG_FILE.exists():
        return []
    limit = max(1, min(limit, 500))
    lines = config.TOOL_LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    entries: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries
