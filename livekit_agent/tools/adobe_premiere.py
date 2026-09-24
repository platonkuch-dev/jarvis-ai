"""Direct scripting control of Adobe Premiere Pro via adobe_cep_bridge --
mirrors tools/adobe_after_effects.py exactly (same bridge, same
whitelist-only op pattern: fixed ExtendScript templates with validated
arguments, never LLM-authored script).

NOTE: written against Premiere Pro's documented ExtendScript object model
(app.project / Sequence / Time) but NOT exercised against a live instance in
this environment -- this machine's Premiere Pro install hit a Creative Cloud
"you no longer have access to this app" gate independent of anything in this
bridge (a subscription/licensing issue, not a bug here) and closed itself
before verification could run. The bridge plumbing itself (installer.py,
client.py) IS proven end-to-end against After Effects on this same machine,
so once Premiere's access issue is resolved: launch it, open Window >
Extensions > Jarvis Bridge once, and report back the exact error text if any
of these calls don't match -- Time/Sequence property names are the most
likely thing to need a tweak.
"""

from __future__ import annotations

import json
from typing import Literal

from livekit.agents import RunContext, function_tool

import config
from adobe_cep_bridge import PREMIERE_PORT, client
from tools._logging import log_call
from tools.registry import register_impl, register_tool


def _jsx_string(s: str) -> str:
    return json.dumps(s)


async def _eval(script: str) -> str:
    return await client.eval_script(PREMIERE_PORT, script)


async def _get_info() -> str:
    # ExtendScript has no global JSON object -- build a pipe-delimited
    # string instead of JSON.stringify (confirmed empty/absent in After
    # Effects' ExtendScript engine live; Premiere shares the same engine).
    result = await _eval("""
        (function() {
            var proj = app.project;
            var seq = proj.activeSequence;
            var seqName = seq ? seq.name : "";
            var videoTracks = seq ? seq.videoTracks.numTracks : 0;
            var audioTracks = seq ? seq.audioTracks.numTracks : 0;
            return proj.name + "|" + seqName + "|" + videoTracks + "|" + audioTracks;
        })();
    """)
    project_name, seq_name, video_tracks, audio_tracks = result.split("|")
    lines = [
        f"Проект: {project_name}",
        f"Активная последовательность: {seq_name or '(нет)'}",
        f"Видео-дорожек: {video_tracks}, аудио-дорожек: {audio_tracks}",
    ]
    return "\n".join(lines)


async def _import_media(path: str) -> str:
    result = await _eval(f"""
        (function() {{
            var ok = app.project.importFiles([{_jsx_string(path)}]);
            return ok ? "ok" : "failed";
        }})();
    """)
    if result != "ok":
        raise RuntimeError(f"Premiere отказался импортировать файл: {path}")
    return f"Импортировано в проект: {path}"


async def _add_marker(seconds: float, comment: str) -> str:
    result = await _eval(f"""
        (function() {{
            var seq = app.project.activeSequence;
            if (!seq) return "no_sequence";
            var marker = seq.markers.createMarker({float(seconds)});
            if (marker && {_jsx_string(comment)}) marker.comments = {_jsx_string(comment)};
            return "ok";
        }})();
    """)
    if result == "no_sequence":
        return "Нет активной последовательности -- откройте её в Premiere сначала."
    return f"Маркер добавлен на {seconds}с" + (f" («{comment}»)" if comment else "")


async def _save_project() -> str:
    await _eval("app.project.save();")
    return "Проект сохранён."


PremiereAction = Literal["info", "import_media", "add_marker", "save"]


@register_impl("premiere_control")
@log_call("premiere_control")
async def _premiere_control(
    *,
    action: str,
    path: str = "",
    seconds: float = 0.0,
    comment: str = "",
) -> dict:
    if config.SYSTEM != "Windows":
        return {"status": "error", "message": "Автоматизация Premiere Pro доступна только на Windows."}

    try:
        if action == "info":
            message = await _get_info()
        elif action == "import_media":
            if not path:
                return {"status": "error", "message": "Не указан путь к файлу."}
            message = await _import_media(path)
        elif action == "add_marker":
            message = await _add_marker(seconds, comment)
        elif action == "save":
            message = await _save_project()
        else:
            return {"status": "error", "message": f"Неизвестное действие premiere_control: «{action}»."}
        return {"status": "ok", "message": message}
    except Exception as exc:
        return {
            "status": "error",
            "message": (
                f"Ошибка Premiere Pro («{action}»): {exc}. Premiere Pro запущен, а панель "
                "Jarvis Bridge открыта хотя бы раз (Окно > Расширения > Jarvis Bridge)?"
            ),
        }


@register_tool
@function_tool
async def premiere_control(
    context: RunContext,
    action: PremiereAction,
    path: str = "",
    seconds: float = 0.0,
    comment: str = "",
) -> str:
    """Control Adobe Premiere Pro directly through its scripting API instead
    of clicking through the UI. Requires Premiere Pro to already be running
    with the Jarvis Bridge extension panel loaded at least once (Window >
    Extensions > Jarvis Bridge) -- ask the user to do this once if a call
    fails saying the bridge isn't reachable. For anything beyond this small
    op set (trimming clips, exporting), use use_computer instead for now.

    Args:
        action: "info" about the active project/sequence; "import_media" a
            file at `path` into the project; "add_marker" on the active
            sequence at `seconds` with optional `comment`; "save" the project.
        path: File path, for "import_media".
        seconds: Timeline position in seconds, for "add_marker".
        comment: Marker text, for "add_marker".
    """
    result = await _premiere_control(action=action, path=path, seconds=seconds, comment=comment)
    return result["message"]
