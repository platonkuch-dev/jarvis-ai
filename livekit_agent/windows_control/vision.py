"""
Level 4 — Computer Vision fallback.

Last resort for locating a UI element: neither the OS API (Level 1) nor UI
Automation (Level 2) could find it — usually because the app renders its own
custom-drawn widgets with no real accessibility tree (games, some Electron
apps, canvas-based UIs). We take a screenshot (optionally cropped to a
window's rectangle), hand it to Claude (the only LLM this project is
configured with — see config.ANTHROPIC_API_KEY), and ask for pixel coordinates.

This is deliberately NOT used for every action — router.py only reaches this
after Levels 1-3 have already failed, per the project's own priority rule:
"Native API -> UI Automation -> Keyboard -> Mouse -> Vision".
"""
from __future__ import annotations

import io
import re

import anthropic

import config

# Vision fallback is a synchronous step inside a live voice tool call --
# the SDK default (10 minutes) would leave Джарвис hung with no user-facing
# feedback on a slow/unresponsive API.
_VISION_TIMEOUT_S = 20.0

try:
    import pyautogui
    _PYAUTOGUI = True
except ImportError:  # pragma: no cover
    _PYAUTOGUI = False


def capture(region: tuple[int, int, int, int] | None = None) -> tuple[bytes, tuple[int, int]]:
    """Screenshot the full screen or a (left, top, right, bottom) region.
    Returns (png_bytes, (width, height)) where width/height are of the
    captured image — needed to translate model coordinates back correctly."""
    if not _PYAUTOGUI:
        raise RuntimeError("pyautogui not installed.")
    if region:
        left, top, right, bottom = region
        w, h = max(1, right - left), max(1, bottom - top)
        img = pyautogui.screenshot(region=(left, top, w, h))
    else:
        img = pyautogui.screenshot()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), img.size


def _locate_with_claude(image_bytes: bytes, prompt: str) -> str | None:
    if config.SUBSCRIPTION_MODE:
        import cc_agent

        return cc_agent.ask_sync(prompt, images=[cc_agent.image_block(image_bytes, "image/png")],
                                 model=config.CLAUDE_CODE_AGENT_MODEL, timeout=60)
    if not config.ANTHROPIC_API_KEY:
        return None
    import base64

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, timeout=_VISION_TIMEOUT_S)
    msg = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=64,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png",
                    "data": base64.b64encode(image_bytes).decode("utf-8"),
                }},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    import usage

    usage.record_response(config.ANTHROPIC_MODEL, msg.usage, source="vision")
    return msg.content[0].text


def locate_element(
    description: str,
    region: tuple[int, int, int, int] | None = None,
) -> tuple[int, int] | None:
    """
    Find a UI element by natural-language description via Claude's vision.
    Returns absolute SCREEN coordinates (already offset by `region`, if given)
    or None if not found / no API key configured.
    """
    if not config.ANTHROPIC_API_KEY and not config.SUBSCRIPTION_MODE:
        print("[Vision] No Anthropic API key configured — vision fallback unavailable.")
        return None

    try:
        image_bytes, (w, h) = capture(region=region)
        prompt = (
            f"This is a screenshot of a {w}x{h} pixel area. "
            f"Locate the UI element described as: '{description}'. "
            f"Reply with ONLY the center coordinates as: x,y "
            f"(relative to THIS image, top-left is 0,0). "
            f"If the element is not visible, reply: NOT_FOUND"
        )

        try:
            text = _locate_with_claude(image_bytes, prompt)
        except Exception as e:
            print(f"[Vision] Claude locate failed: {e}")
            return None

        text = (text or "").strip()
        if "NOT_FOUND" in text.upper():
            return None

        match = re.search(r"(\d+)\s*,\s*(\d+)", text)
        if not match:
            return None
        x, y = int(match.group(1)), int(match.group(2))

        if region:
            x += region[0]
            y += region[1]
        return x, y

    except Exception as e:
        print(f"[Vision] locate_element failed: {e}")
        return None
