"""Look at the webcam: pops the camera preview out of the HUD bar, grabs one
frame once it's warmed up, asks Claude what's in it, and speaks the answer.

Split across two processes like everything else in the HUD (see
camera_bridge.py's docstring): this module runs in the voice-worker process
and only ever talks to camera_bridge's two JSON files. hud_bar.py's
CameraPanel is the only thing that actually opens a QCamera -- that keeps
device access in one place and lets the panel animate in Qt's own event
loop instead of trying to drive a Qt animation from here.

Privacy: unlike take_screenshot() (which keeps the file), the captured frame
is deleted the moment this function is done with it, win or lose -- a face/
room photo is a different order of sensitive than a desktop screenshot, and
nothing here needs to keep it around after the model has answered.
"""

from __future__ import annotations

import asyncio
import base64
import time

import anthropic
from livekit.agents import RunContext, function_tool

import camera_bridge
import config
import usage
from tools import runtime
from tools._logging import log_call
from tools.registry import register_impl, register_tool

def _next_seq() -> int:
    # A nanosecond timestamp rather than a small incrementing counter: this
    # module and panel.py's camera tab both write commands to the same
    # camera_command.json (see camera_bridge.py), from two separate
    # processes -- two independent small counters could collide on the same
    # seq value, which would let one caller pick up a result meant for the
    # other. Two different processes producing the same nanosecond is not a
    # realistic risk on any clock this runs on.
    return time.time_ns()


async def _wait_for_result(seq: int, timeout: float) -> dict | None:
    """Polls camera_result.json until it carries this exact seq, or times out.
    ~100ms between reads: fast enough to feel responsive, cheap enough to
    poll from an asyncio task without a thread."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = camera_bridge.read_result()
        if result is not None and result.get("seq") == seq:
            return result
        await asyncio.sleep(0.1)
    return None


async def _ask_about_image(image_path, question: str) -> str:
    image_bytes = await asyncio.to_thread(lambda: image_path.read_bytes())
    b64 = base64.b64encode(image_bytes).decode("ascii")

    prompt = (
        f"{question}" if question else
        "Кратко опиши, что видно на этом кадре с веб-камеры."
    )
    system = (
        "Ты — зрение голосового ассистента Jarvis, смотрящего через веб-камеру "
        "пользователя. Отвечай по-русски, естественно и коротко (1-3 предложения) "
        "— ответ будет озвучен вслух, а не прочитан. Опиши только то, что реально "
        "видно на кадре; если кадр тёмный, размытый или пустой, так и скажи."
    )
    if config.SUBSCRIPTION_MODE:
        import cc_agent

        text = await cc_agent.ask(prompt, system=system, images=[cc_agent.image_block(image_bytes)],
                                  model=config.CLAUDE_CODE_AGENT_MODEL)
        return text.strip() or "Не удалось разобрать, что на кадре."
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    msg = await client.messages.create(
        model=config.CAMERA_MODEL,
        max_tokens=400,
        system=system,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    usage.record_response(config.CAMERA_MODEL, msg.usage, source="camera")
    parts = [c.text for c in msg.content if getattr(c, "type", None) == "text"]
    return "".join(parts).strip() or "Не удалось разобрать, что на кадре."


@register_impl("look_at_camera")
@log_call("look_at_camera")
async def _look_at_camera(*, question: str = "") -> dict:
    open_seq = _next_seq()
    camera_bridge.write_command(open_seq, "open")
    opened = await _wait_for_result(open_seq, config.CAMERA_OPEN_TIMEOUT_S)

    if opened is None:
        # The HUD process never answered at all (not running, or stuck) --
        # nothing was actually opened on screen, so there is nothing to close.
        return {"status": "error", "message": "Панель камеры не ответила — похоже, HUD сейчас не запущен."}

    try:
        if opened["status"] == "no_camera":
            return {"status": "error", "message": "Камера не найдена на этом компьютере."}
        if opened["status"] == "error":
            return {"status": "error", "message": f"Не удалось включить камеру: {opened.get('message') or 'неизвестная ошибка'}."}

        # Say this now, not after: the capture + vision call below take a
        # couple of seconds, and the project's own UX principle (see README)
        # is to never leave the user in silence while something's visibly
        # happening on screen.
        asyncio.create_task(runtime.say("Смотрю через камеру, сэр."))

        await asyncio.sleep(config.CAMERA_WARMUP_S)

        capture_seq = _next_seq()
        camera_bridge.write_command(capture_seq, "capture")
        captured = await _wait_for_result(capture_seq, config.CAMERA_CAPTURE_TIMEOUT_S)

        if captured is None or captured["status"] == "error":
            reason = (captured or {}).get("message") or "таймаут"
            return {"status": "error", "message": f"Не удалось сделать кадр с камеры: {reason}."}

        from pathlib import Path
        image_path = Path(captured["path"])
        try:
            message = await _ask_about_image(image_path, question)
            return {"status": "ok", "message": message}
        finally:
            try:
                image_path.unlink(missing_ok=True)
            except Exception:
                pass
    finally:
        # Whenever "open" actually opened (or animated in) the panel --
        # including the no_camera/error cases, which still show a message on
        # screen -- always ask it to close again. Success or failure, the
        # panel should never stay up longer than this one request.
        camera_bridge.write_command(_next_seq(), "close")


@register_tool
@function_tool
async def look_at_camera(context: RunContext, question: str = "") -> str:
    """Open the webcam, look at what it sees, and describe it out loud.

    Use this when the user asks to open/check the camera, or to look at
    something through the camera (e.g. "открой камеру", "посмотри что на
    камере", "what am I holding?", "look at this"). The camera opens with a
    short on-screen animation, grabs a single frame once it's warmed up, and
    closes again right after — it is never left running.

    Args:
        question: What to look for or answer about the frame, in the user's
            own words (e.g. "what am I holding", "is the door open"). Leave
            empty for a general "what do you see" description.
    """
    result = await _look_at_camera(question=question)
    return result["message"]
