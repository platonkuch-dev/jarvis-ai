"""
Reconnect-loop support for JarvisLive.run(): API key lookup, TaskGroup
exception classification, and system-prompt/transcript loading.

Split out of main.py (Stage 2 module split, see REWORK_PLAN.md) with no
behavior changes -- these were previously module-level functions in main.py.
"""
from __future__ import annotations

import re

import sounddevice as sd

from core import runtime_config
from core.path_utils import resource_path

PROMPT_PATH = resource_path("core/prompt.txt")


def get_api_key() -> str:
    return runtime_config.get_config()["gemini_api_key"]


def flatten_exceptions(exc: BaseException) -> list[BaseException]:
    """
    asyncio.TaskGroup wraps whatever its child tasks raise in a Base
    ExceptionGroup/ExceptionGroup — str(group) is always the generic
    "unhandled errors in a TaskGroup (N sub-exceptions)", it never contains
    the real error message. Every keyword-based error classification in
    run() (network vs. invalid API key vs. audio device vs. everything
    else) was matching against that generic string and therefore NEVER
    matched for any error raised from inside the TaskGroup body — only
    errors raised before the TaskGroup was entered (e.g. connect() failing
    on a bad key) happened to classify correctly by accident. This walks
    nested groups and returns the real leaf exceptions so classification
    can look at what actually broke.
    """
    if isinstance(exc, (ExceptionGroup, BaseExceptionGroup)):
        leaves: list[BaseException] = []
        for sub in exc.exceptions:
            leaves.extend(flatten_exceptions(sub))
        return leaves
    return [exc]


def is_audio_device_error(exc: BaseException) -> bool:
    """True if `exc` is sounddevice/PortAudio failing to open or write to a
    device — e.g. no driver installed, or the default device was unplugged.
    This is a local hardware/driver problem, not a Gemini/network problem,
    so it must NOT be classified as a network error (see run()'s handling)."""
    if isinstance(exc, sd.PortAudioError):
        return True
    name = type(exc).__name__
    return "PortAudioError" in name


def load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are JARVIS. Talk the way Claude talks: warm but direct, thoughtful, no theatrics. "
            "Be concise, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )


_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)


def clean_transcript(text: str) -> str:
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()
