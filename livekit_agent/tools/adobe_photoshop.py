"""Direct COM automation for Adobe Photoshop (Windows only) -- precise,
non-visual control where clicking through the UI (tools/computer_use.py)
would be slower and more error-prone than calling Photoshop's own scripting
API directly, matching the "Прямой API приложений" layer of the requested
architecture.

Deliberately does NOT expose arbitrary ExtendScript execution -- same
whitelist-only principle as tools/dev.py's run_predefined_script. The one
open-ended action, "run_action", can only trigger a Photoshop Action the
user has already recorded by name in Photoshop's own Actions panel; Джарвис
can pick one by name, it can never write or send new script code itself.

Premiere Pro and After Effects don't expose this kind of lightweight COM
automation on Windows (their scripting bridge needs a CEP/UXP debug
connection, a bigger separate integration) -- use tools/computer_use.py's
use_computer for those today.

NOTE: written against Adobe's documented Photoshop COM object model
(Application / Document / *SaveOptions ProgIDs) but not exercised against a
real Photoshop install in this environment -- if a specific call 400s/COM-
errors on your machine, the error message is passed straight through, so
report it back and the exact ProgID/method can be adjusted.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

from livekit.agents import RunContext, function_tool

import config
from tools._logging import log_call
from tools.registry import register_impl, register_tool


def _get_app():
    import win32com.client

    app = win32com.client.gencache.EnsureDispatch("Photoshop.Application")
    app.Visible = True
    return app


def _require_document(app):
    if app.Documents.Count == 0:
        raise RuntimeError("Нет открытых документов в Photoshop.")
    return app.ActiveDocument


def _open(path: str) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        return f"Файл не найден: {path}"
    app = _get_app()
    app.Open(str(p))
    return f"Открыто в Photoshop: {p.name}"


def _close(save: bool) -> str:
    import win32com.client

    app = _get_app()
    doc = _require_document(app)
    name = doc.Name
    opt = (
        win32com.client.constants.psSaveChanges
        if save else win32com.client.constants.psDoNotSaveChanges
    )
    doc.Close(opt)
    return f"Закрыто: {name}"


def _info() -> str:
    app = _get_app()
    doc = _require_document(app)
    return (
        f"Документ: {doc.Name}\n"
        f"Размер: {doc.Width}x{doc.Height}, {doc.Resolution} DPI\n"
        f"Режим: {doc.Mode}\n"
        f"Слоёв: {doc.Layers.Count}"
    )


def _resize(width: int, height: int, resolution: float | None) -> str:
    import win32com.client

    app = _get_app()
    doc = _require_document(app)
    app.Preferences.RulerUnits = win32com.client.constants.psPixels
    if resolution:
        doc.ResizeImage(width, height, resolution)
    else:
        doc.ResizeImage(width, height)
    return f"Размер изменён: {width}x{height}px"


def _flatten() -> str:
    app = _get_app()
    doc = _require_document(app)
    doc.Flatten()
    return f"Слои объединены: {doc.Name}"


def _save() -> str:
    app = _get_app()
    doc = _require_document(app)
    doc.Save()
    return f"Сохранено: {doc.Name}"


def _save_as_jpeg(path: str, quality: int) -> str:
    import win32com.client

    app = _get_app()
    doc = _require_document(app)
    opts = win32com.client.Dispatch("Photoshop.JPEGSaveOptions")
    opts.Quality = max(1, min(12, quality))
    p = Path(path).expanduser()
    doc.SaveAs(str(p), opts, True)
    return f"Сохранено как JPEG: {p}"


def _save_as_png(path: str) -> str:
    import win32com.client

    app = _get_app()
    doc = _require_document(app)
    opts = win32com.client.Dispatch("Photoshop.PNGSaveOptions")
    p = Path(path).expanduser()
    doc.SaveAs(str(p), opts, True)
    return f"Сохранено как PNG: {p}"


def _run_action(action_set: str, action_name: str) -> str:
    app = _get_app()
    app.DoAction(action_name, action_set)
    return f"Выполнено действие «{action_name}» из набора «{action_set}»."


PhotoshopAction = Literal[
    "open", "close", "info", "resize", "flatten", "save",
    "save_as_jpeg", "save_as_png", "run_action",
]


@register_impl("photoshop_control")
@log_call("photoshop_control")
async def _photoshop_control(
    *,
    action: str,
    path: str = "",
    save: bool = False,
    quality: int = 10,
    width: int | None = None,
    height: int | None = None,
    resolution: float | None = None,
    action_set: str = "",
    action_name: str = "",
) -> dict:
    if config.SYSTEM != "Windows":
        return {"status": "error", "message": "Автоматизация Photoshop доступна только на Windows."}

    def _dispatch() -> str:
        import pythoncom

        pythoncom.CoInitialize()
        try:
            if action == "open":
                if not path:
                    return "Не указан путь к файлу."
                return _open(path)
            if action == "close":
                return _close(save)
            if action == "info":
                return _info()
            if action == "resize":
                if not width or not height:
                    return "Не указаны width и height."
                return _resize(int(width), int(height), resolution)
            if action == "flatten":
                return _flatten()
            if action == "save":
                return _save()
            if action == "save_as_jpeg":
                if not path:
                    return "Не указан путь для сохранения."
                return _save_as_jpeg(path, quality)
            if action == "save_as_png":
                if not path:
                    return "Не указан путь для сохранения."
                return _save_as_png(path)
            if action == "run_action":
                if not action_set or not action_name:
                    return "Не указаны action_set и action_name."
                return _run_action(action_set, action_name)
            return f"Неизвестное действие photoshop_control: «{action}»."
        finally:
            pythoncom.CoUninitialize()

    try:
        message = await asyncio.to_thread(_dispatch)
        return {"status": "ok", "message": message}
    except Exception as exc:
        return {
            "status": "error",
            "message": f"Ошибка Photoshop («{action}»): {exc}. Photoshop установлен и запущен?",
        }


@register_tool
@function_tool
async def photoshop_control(
    context: RunContext,
    action: PhotoshopAction,
    path: str = "",
    save: bool = False,
    quality: int = 10,
    width: int | None = None,
    height: int | None = None,
    resolution: float | None = None,
    action_set: str = "",
    action_name: str = "",
) -> str:
    """Control Adobe Photoshop directly through its scripting API (exact and
    fast) instead of clicking through the UI. Launches Photoshop if it isn't
    already running.

    Args:
        action: "open" a file at `path`; "close" the active document;
            "info" about the active document; "resize" to width/height (px);
            "flatten" all layers; "save" in place; "save_as_jpeg"/
            "save_as_png" to `path`; "run_action" to trigger a Photoshop
            Action the user has already recorded (by `action_set` + `action_name`
            exactly as named in Photoshop's Actions panel) -- this can never
            run arbitrary script, only a macro that already exists there.
        path: File path, for "open"/"save_as_jpeg"/"save_as_png".
        save: Save changes before closing, for "close".
        quality: JPEG quality 1-12, for "save_as_jpeg".
        width: Target width in pixels, for "resize".
        height: Target height in pixels, for "resize".
        resolution: Target DPI, for "resize" (omit to keep current).
        action_set: The Action Set name in Photoshop's Actions panel, for "run_action".
        action_name: The Action's name within that set, for "run_action".
    """
    result = await _photoshop_control(
        action=action, path=path, save=save, quality=quality, width=width,
        height=height, resolution=resolution, action_set=action_set, action_name=action_name,
    )
    return result["message"]
