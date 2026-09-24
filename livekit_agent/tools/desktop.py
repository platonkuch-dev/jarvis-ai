"""Desktop tools: wallpaper, and organizing/cleaning/inspecting the Desktop
folder.

Deliberately does NOT include the old project's AI-powered "task" action,
which asked an LLM to generate arbitrary Python and executed it in a
sandbox -- that's the same category of risk as the hacker_terminal.py /
self_extend.py modules already excluded from this port (whitelist-only,
no dynamically generated code ever gets executed).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal

from livekit.agents import RunContext, function_tool

import config
from tools._logging import log_call
from tools.registry import register_impl, register_tool

_OS = config.SYSTEM

_FILE_TYPE_MAP = {
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".ico", ".heic"},
    "Documents": {".pdf", ".doc", ".docx", ".txt", ".xls", ".xlsx",
                  ".ppt", ".pptx", ".csv", ".odt", ".ods", ".odp"},
    "Videos": {".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".m4v"},
    "Music": {".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"},
    "Code": {".py", ".js", ".ts", ".html", ".css", ".json", ".xml",
             ".cpp", ".java", ".cs", ".go", ".rs", ".sh", ".php"},
    "Executables": {".exe", ".msi", ".bat", ".cmd", ".sh", ".appimage", ".deb", ".rpm"},
}
_SKIP_EXTENSIONS = {
    "Windows": {".lnk", ".url"},
    "Darwin": {".webloc"},
    "Linux": {".desktop"},
}


def _get_desktop() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_DESKTOP_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Desktop"


def _format_size(size: int) -> str:
    return f"{size / 1024:.1f} КБ" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} МБ"


def _set_wallpaper(image_path: str) -> str:
    path = Path(image_path).expanduser().resolve()
    if not path.exists():
        return f"Изображение не найдено: {image_path}"
    if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}:
        return f"Неподдерживаемый формат: {path.suffix}. Используйте jpg, png, bmp или webp."

    if _OS == "Windows":
        import ctypes
        if path.suffix.lower() in {".webp", ".png"}:
            try:
                from PIL import Image
                bmp_path = Path(tempfile.mktemp(suffix=".bmp"))
                Image.open(path).convert("RGB").save(bmp_path, "BMP")
                path = bmp_path
            except ImportError:
                pass
        ctypes.windll.user32.SystemParametersInfoW(20, 0, str(path), 3)
        return f"Обои установлены: {path.name}"

    if _OS == "Darwin":
        script = (
            f'tell application "System Events" to tell every desktop to '
            f'set picture to POSIX file "{path}"'
        )
        subprocess.run(["osascript", "-e", script], capture_output=True)
        return f"Обои установлены: {path.name}"

    desktop_env = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    uri = f"file://{path}"
    if "gnome" in desktop_env or "unity" in desktop_env:
        subprocess.run(["gsettings", "set", "org.gnome.desktop.background", "picture-uri", uri], capture_output=True)
        subprocess.run(["gsettings", "set", "org.gnome.desktop.background", "picture-uri-dark", uri], capture_output=True)
        return f"Обои установлены: {path.name}"
    if "xfce" in desktop_env:
        subprocess.run(
            ["xfconf-query", "-c", "xfce4-desktop", "-p",
             "/backdrop/screen0/monitor0/workspace0/last-image", "-s", str(path)],
            capture_output=True,
        )
        return f"Обои установлены: {path.name}"
    result = subprocess.run(["feh", "--bg-scale", str(path)], capture_output=True)
    if result.returncode != 0:
        return f"Не удалось автоматически установить обои на {desktop_env or 'этом окружении'}."
    return f"Обои установлены: {path.name}"


def _set_wallpaper_from_url(url: str) -> str:
    import urllib.request

    suffix = Path(url.split("?")[0]).suffix or ".jpg"
    tmp = Path(tempfile.mktemp(suffix=suffix))
    try:
        urllib.request.urlretrieve(url, str(tmp))  # noqa: S310 - user-supplied wallpaper URL, saved to temp only
        return _set_wallpaper(str(tmp))
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass


def _get_current_wallpaper() -> str:
    if _OS == "Windows":
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop")
        val, _ = winreg.QueryValueEx(key, "Wallpaper")
        winreg.CloseKey(key)
        return f"Текущие обои: {val}"

    if _OS == "Darwin":
        result = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to get picture of desktop 1'],
            capture_output=True, text=True,
        )
        return f"Текущие обои: {result.stdout.strip()}"

    desktop_env = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    if "gnome" in desktop_env or "unity" in desktop_env:
        result = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.background", "picture-uri"],
            capture_output=True, text=True,
        )
        return f"Текущие обои: {result.stdout.strip()}"
    return "Получение пути обоев не поддерживается для этого окружения."


def _organize_desktop(mode: str) -> str:
    desktop = _get_desktop()
    skip_exts = _SKIP_EXTENSIONS.get(_OS, set())
    moved, skipped = [], []

    for item in desktop.iterdir():
        if item.is_dir() or item.name.startswith("."):
            continue
        if item.suffix.lower() in skip_exts:
            continue

        if mode == "by_date":
            folder_name = datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m")
        else:
            ext = item.suffix.lower()
            folder_name = "Others"
            for folder, exts in _FILE_TYPE_MAP.items():
                if ext in exts:
                    folder_name = folder
                    break

        target_dir = desktop / folder_name
        target_dir.mkdir(exist_ok=True)
        new_path = target_dir / item.name
        if new_path.exists():
            skipped.append(item.name)
            continue
        shutil.move(str(item), str(new_path))
        moved.append(f"{item.name} -> {folder_name}/")

    result = f"Рабочий стол упорядочен ({mode}): перемещено {len(moved)}."
    if moved:
        result += "\n" + "\n".join(moved[:8])
        if len(moved) > 8:
            result += f"\n... и ещё {len(moved) - 8}."
    if skipped:
        result += f"\nПропущено {len(skipped)} (конфликт имён)."
    return result


def _list_desktop() -> str:
    desktop = _get_desktop()
    items = []
    for item in sorted(desktop.iterdir()):
        if item.name.startswith("."):
            continue
        if item.is_dir():
            try:
                count = len(list(item.iterdir()))
            except PermissionError:
                count = "?"
            items.append(f"[папка] {item.name}/ ({count})")
        else:
            items.append(f"[файл] {item.name} ({_format_size(item.stat().st_size)})")
    if not items:
        return "Рабочий стол пуст."
    return f"Рабочий стол ({len(items)}):\n" + "\n".join(items)


def _clean_desktop() -> str:
    desktop = _get_desktop()
    skip_exts = _SKIP_EXTENSIONS.get(_OS, set())
    today = datetime.now().strftime("%Y-%m-%d")
    archive_dir = desktop / f"Архив рабочего стола {today}"
    archive_dir.mkdir(exist_ok=True)

    moved = 0
    for item in desktop.iterdir():
        if item.is_dir() or item.name.startswith("."):
            continue
        if item.suffix.lower() in skip_exts:
            continue
        new_path = archive_dir / item.name
        if not new_path.exists():
            shutil.move(str(item), str(new_path))
            moved += 1
    return f"Рабочий стол очищен: {moved} файлов перемещено в «{archive_dir.name}»."


def _get_desktop_stats() -> str:
    desktop = _get_desktop()
    files = [i for i in desktop.iterdir() if i.is_file()]
    folders = [i for i in desktop.iterdir() if i.is_dir()]
    total_size = sum(f.stat().st_size for f in files if f.exists())
    return (
        f"Статистика рабочего стола ({_OS}):\n"
        f"  Файлов: {len(files)}\n"
        f"  Папок: {len(folders)}\n"
        f"  Размер: {_format_size(total_size)}\n"
        f"  Путь: {desktop}"
    )


DesktopAction = Literal[
    "wallpaper", "wallpaper_url", "current_wallpaper",
    "organize", "clean", "list", "stats",
]


@register_impl("desktop_control")
@log_call("desktop_control")
async def _desktop_control(
    *,
    action: str,
    path: str = "",
    url: str = "",
    mode: str = "by_type",
) -> dict:
    def _run() -> str:
        if action == "wallpaper":
            if not path:
                return "Не указан путь к изображению."
            return _set_wallpaper(path)
        if action == "wallpaper_url":
            if not url:
                return "Не указан URL изображения."
            return _set_wallpaper_from_url(url)
        if action == "current_wallpaper":
            return _get_current_wallpaper()
        if action == "organize":
            return _organize_desktop(mode)
        if action == "clean":
            return _clean_desktop()
        if action == "list":
            return _list_desktop()
        if action == "stats":
            return _get_desktop_stats()
        return f"Неизвестное действие desktop_control: «{action}»."

    try:
        message = await asyncio.to_thread(_run)
        return {"status": "ok", "message": message}
    except Exception as exc:
        return {"status": "error", "message": f"Ошибка desktop_control «{action}»: {exc}"}


@register_tool
@function_tool
async def desktop_control(
    context: RunContext,
    action: DesktopAction,
    path: str = "",
    url: str = "",
    mode: str = "by_type",
) -> str:
    """Manage the desktop wallpaper and the Desktop folder's contents.

    Args:
        action: "wallpaper" to set it from a local `path`; "wallpaper_url" to
            set it from an image `url`; "current_wallpaper" to report it;
            "organize" to sort loose Desktop files into folders (by `mode`);
            "clean" to archive all loose files into a dated folder; "list" to
            show Desktop contents; "stats" for a file/folder/size summary.
        path: Local image file path, for "wallpaper".
        url: Image URL, for "wallpaper_url".
        mode: "by_type" (default) or "by_date", for "organize".
    """
    result = await _desktop_control(action=action, path=path, url=url, mode=mode)
    return result["message"]
