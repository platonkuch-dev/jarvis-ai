"""
Level 4 — Computer Vision fallback.

Last resort for locating a UI element: neither the OS API (Level 1) nor UI
Automation (Level 2) could find it — usually because the app renders its own
custom-drawn widgets with no real accessibility tree (games, some Electron
apps, canvas-based UIs). We take a screenshot (optionally cropped to a
window's rectangle), hand it to whichever vision model has a configured key
(Claude preferred, Gemini fallback — matching the pattern already used
elsewhere in this codebase, e.g. actions/computer_settings.py's intent
detector), and ask for pixel coordinates.

This is deliberately NOT used for every action — router.py only reaches this
after Levels 1-3 have already failed, per the project's own priority rule:
"Native API -> UI Automation -> Keyboard -> Mouse -> Vision".
"""
from __future__ import annotations

import io
import re

from core.config import CLAUDE_MODEL, GEMINI_LITE_MODEL
from core.runtime_config import get_gemini_api_key as _get_gemini_key, get_claude_api_key as _get_claude_key

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
    claude_key = _get_claude_key()
    if not claude_key:
        return None
    import base64
    import anthropic

    client = anthropic.Anthropic(api_key=claude_key)
    msg = client.messages.create(
        model=CLAUDE_MODEL,
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
    return msg.content[0].text


def _locate_with_gemini(image_bytes: bytes, prompt: str) -> str | None:
    api_key = _get_gemini_key()
    if not api_key:
        return None
    from google import genai
    from google.genai import types as gtypes

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=GEMINI_LITE_MODEL,
        contents=[gtypes.Part.from_bytes(data=image_bytes, mime_type="image/png"), prompt],
    )
    return response.text


def locate_element(
    description: str,
    region: tuple[int, int, int, int] | None = None,
) -> tuple[int, int] | None:
    """
    Find a UI element by natural-language description via vision model.
    Returns absolute SCREEN coordinates (already offset by `region`, if given)
    or None if not found / no vision key configured.
    """
    if not (_get_claude_key() or _get_gemini_key()):
        print("[Vision] No Claude/Gemini API key configured — vision fallback unavailable.")
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

        text = None
        try:
            text = _locate_with_claude(image_bytes, prompt)
        except Exception as e:
            print(f"[Vision] Claude locate failed ({e}) — falling back to Gemini.")
        if text is None:
            text = _locate_with_gemini(image_bytes, prompt)

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
