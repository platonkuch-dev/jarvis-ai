"""Focused checks for the cloud-independent local text-command fallback."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core import fast_path


class FakeUI:
    def __init__(self):
        self.logs: list[str] = []

    def write_log(self, text: str) -> None:
        self.logs.append(text)


class FakeTTS:
    def __init__(self):
        self.spoken: list[str] = []

    def speak(self, text: str) -> None:
        self.spoken.append(text)


async def check_local_fallback() -> None:
    original_route = fast_path.fast_route
    try:
        ui = FakeUI()
        tts = FakeTTS()
        assistant = SimpleNamespace(
            ui=ui,
            _intent_classifier=SimpleNamespace(is_ready=False),
            _voice_pipeline=SimpleNamespace(tts=tts),
            set_speaking=lambda _value: None,
        )
        fast_path.fast_route = lambda _text: fast_path.RouteResult(
            matched=True, rule_name="test", success=True, message="Done locally."
        )
        assert await fast_path.handle_local_text(assistant, "test command")
        assert ui.logs == ["Jarvis: Done locally."]
        assert tts.spoken == ["Done locally."]

        fast_path.fast_route = lambda _text: fast_path.RouteResult(matched=False)
        assert not await fast_path.handle_local_text(assistant, "unknown command")
        print("[PASS] local match is handled and an unknown command falls through")
    finally:
        fast_path.fast_route = original_route


asyncio.run(check_local_fallback())