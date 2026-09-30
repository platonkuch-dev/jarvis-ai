"""File-system tools: list/create/move/copy/rename/delete/read/write/find
on every fixed drive (`_SAFE_ROOTS`). System folders (Windows, Program
Files, ProgramData, drive roots) are read-only here, so a misheard path
can't break Windows; changing them goes through PowerShell and pc_guard.py's
spoken confirmation. Deletion goes through the Recycle Bin
(send2trash), never a permanent unlink, and a fixed set of "protected"
top-level folders (Desktop/Downloads/Documents/...) can't be deleted outright
-- only their contents can.

This complements tools/system.py's find_and_open_file (search + open) with
the read/write/organize primitives a voice assistant needs for "переименуй
файл", "покажи, что на рабочем столе", "сколько места на диске", etc.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
from datetime import datetime
from pathlib import Path
from typing import Literal

from livekit.agents import RunContext, function_tool

from tools._logging import log_call
from tools.registry import register_impl, register_tool

try:
    import send2trash
    _SEND2TRASH = True
except ImportError:
    _SEND2TRASH = False

_OS = platform.system()

def _fixed_drives() -> list[Path]:
    try:
        import psutil

        return [Path(p.mountpoint) for p in psutil.disk_partitions(all=False)
                if _OS != "Windows" or "fixed" in p.opts]
    except Exception:
        return []


# Full access: the home folder and every fixed drive. System folders stay
# read-only here (see _is_system_path); changing them goes through PowerShell
# and pc_guard's spoken confirmation instead.
_SAFE_ROOTS: list[Path] = [Path.home(), *_fixed_drives()]


def _is_safe_path(target: Path) -> bool:
    try:
        resolved = target.resolve()
        return any(
            resolved == root.resolve() or resolved.is_relative_to(root.resolve())
            for root in _SAFE_ROOTS
        )
    except Exception:
        return False


def _system_roots() -> list[Path]:
    if _OS != "Windows":
        return [Path(p) for p in ("/bin", "/boot", "/etc", "/lib", "/sbin", "/usr", "/System")]
    names = ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
    roots = [Path(os.environ[n]) for n in names if os.environ.get(n)]
    return roots + [Path(os.environ.get("SystemDrive", "C:") + "\\")]


def _is_system_path(target: Path) -> bool:
    """A system folder, or a drive root itself: file_manager won't change these."""
    try:
        resolved = target.resolve()
    except Exception:
        return True
    if resolved.parent == resolved:  # a drive root / "/"
        return True
    return any(
        resolved.is_relative_to(root) for root in _system_roots() if root.parent != root
    )


def _can_modify(target: Path) -> str | None:
    """None if file_manager may change `target`, else the refusal to show."""
    if not _is_safe_path(target):
        return f"Доступ запрещён: {target}"
    if _is_system_path(target):
        return (f"Это системная папка ({target}) — через file_manager её не меняю. "
                "Если это правда нужно, сделай через PowerShell: система спросит подтверждение у пользователя.")
    return None


def _xdg_or_home(env_var: str, folder: str) -> Path:
    if _OS == "Linux":
        xdg = os.environ.get(env_var, "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / folder


def _get_desktop() -> Path:
    return _xdg_or_home("XDG_DESKTOP_DIR", "Desktop")


def _get_downloads() -> Path:
    return _xdg_or_home("XDG_DOWNLOAD_DIR", "Downloads")


def _get_documents() -> Path:
    return _xdg_or_home("XDG_DOCUMENTS_DIR", "Documents")


def _get_pictures() -> Path:
    return _xdg_or_home("XDG_PICTURES_DIR", "Pictures")


def _get_music() -> Path:
    return _xdg_or_home("XDG_MUSIC_DIR", "Music")


def _get_videos() -> Path:
    return _xdg_or_home("XDG_VIDEOS_DIR", "Videos")


def _resolve_path(raw: str) -> Path:
    shortcuts: dict[str, Path] = {
        "desktop": _get_desktop(),
        "downloads": _get_downloads(),
        "documents": _get_documents(),
        "pictures": _get_pictures(),
        "music": _get_music(),
        "videos": _get_videos(),
        "home": Path.home(),
    }
    lower = raw.strip().lower()
    if lower in shortcuts:
        return shortcuts[lower]
    return Path(raw).expanduser()


def _format_size(b: float) -> str:
    for unit in ["Б", "КБ", "МБ", "ГБ", "ТБ"]:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} ПБ"


def _safe_trash(target: Path) -> str:
    if not _SEND2TRASH:
        return "Удаление недоступно: не установлен модуль send2trash."
    send2trash.send2trash(str(target))
    return f"Перемещено в корзину: {target.name}"


def _protected_dirs() -> set[Path]:
    return {
        p.resolve() for p in (
            _get_desktop(), _get_downloads(), _get_documents(),
            _get_pictures(), _get_music(), _get_videos(), Path.home(),
        )
    }


def _list_files(path: str, show_hidden: bool) -> str:
    target = _resolve_path(path)
    if not _is_safe_path(target):
        return f"Доступ запрещён: {target}"
    if not target.exists():
        return f"Путь не найден: {target}"
    if not target.is_dir():
        return f"Это не папка: {target}"

    items = []
    for item in sorted(target.iterdir()):
        if not show_hidden and item.name.startswith("."):
            continue
        if item.is_dir():
            items.append(f"[папка] {item.name}/")
        else:
            items.append(f"[файл] {item.name} ({_format_size(item.stat().st_size)})")

    if not items:
        return f"Папка пуста: {target.name}/"
    return f"Содержимое {target.name}/ ({len(items)}):\n" + "\n".join(items)


def _create_file(path: str, name: str, content: str) -> str:
    base = _resolve_path(path)
    target = (base / name) if name else base
    if refusal := _can_modify(target):
        return refusal
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Файл создан: {target.name}"


def _create_folder(path: str, name: str) -> str:
    base = _resolve_path(path)
    target = (base / name) if name else base
    if refusal := _can_modify(target):
        return refusal
    target.mkdir(parents=True, exist_ok=True)
    return f"Папка создана: {target.name}"


def _delete_file(path: str, name: str) -> str:
    base = _resolve_path(path)
    target = (base / name) if name else base
    if refusal := _can_modify(target):
        return refusal
    if not target.exists():
        return f"Не найдено: {target.name}"
    if target.resolve() in _protected_dirs():
        return f"Это защищённая папка, удалить целиком нельзя: {target.name}"
    return _safe_trash(target)


def _move_file(path: str, name: str, destination: str) -> str:
    base = _resolve_path(path)
    src = (base / name) if name else base
    if not destination:
        return "Не указан путь назначения."
    dst = _resolve_path(destination)

    if not src.exists():
        return f"Источник не найден: {src.name}"
    if refusal := _can_modify(src):
        return refusal
    if refusal := _can_modify(dst):
        return refusal

    if dst.is_dir():
        dst = dst / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"Перемещено: {src.name} -> {dst.parent.name}/"


def _copy_file(path: str, name: str, destination: str) -> str:
    base = _resolve_path(path)
    src = (base / name) if name else base
    if not destination:
        return "Не указан путь назначения."
    dst = _resolve_path(destination)

    if not src.exists():
        return f"Источник не найден: {src.name}"
    if not _is_safe_path(src):
        return f"Доступ запрещён (источник): {src}"
    if refusal := _can_modify(dst):
        return refusal

    if dst.is_dir():
        dst = dst / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(str(src), str(dst))
    else:
        shutil.copy2(str(src), str(dst))
    return f"Скопировано: {src.name} -> {dst.parent.name}/"


def _rename_file(path: str, name: str, new_name: str) -> str:
    base = _resolve_path(path)
    target = (base / name) if name else base
    if refusal := _can_modify(target):
        return refusal
    if not target.exists():
        return f"Не найдено: {target.name}"
    if not new_name:
        return "Не указано новое имя."
    new_path = target.parent / new_name
    if new_path.exists():
        return f"Файл с именем «{new_name}» уже существует."
    target.rename(new_path)
    return f"Переименовано: {target.name} -> {new_name}"


def _read_file(path: str, name: str, max_chars: int) -> str:
    base = _resolve_path(path)
    target = (base / name) if name else base
    if not _is_safe_path(target):
        return f"Доступ запрещён: {target}"
    if not target.exists():
        return f"Файл не найден: {target.name}"
    if not target.is_file():
        return f"Это не файл: {target.name}"
    content = target.read_text(encoding="utf-8", errors="ignore")
    if len(content) > max_chars:
        content = content[:max_chars] + f"\n\n[Обрезано — всего {len(content)} символов]"
    return content


def _write_file(path: str, name: str, content: str, append: bool) -> str:
    base = _resolve_path(path)
    target = (base / name) if name else base
    if refusal := _can_modify(target):
        return refusal
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with open(target, mode, encoding="utf-8") as f:
        f.write(content)
    return f"{'Дописано в' if append else 'Записано в'}: {target.name}"


def _find_files(name: str, extension: str, path: str, max_results: int) -> str:
    search_path = _resolve_path(path)
    if not _is_safe_path(search_path):
        return f"Доступ запрещён: {search_path}"
    if not search_path.exists():
        return f"Путь не найден: {path}"

    results: list[str] = []
    dir_count = 0
    max_dirs = 500

    for item in search_path.rglob("*"):
        if item.is_dir():
            dir_count += 1
            if dir_count > max_dirs:
                break
            continue
        if not item.is_file():
            continue
        if extension and item.suffix.lower() != extension.lower():
            continue
        if name and name.lower() not in item.name.lower():
            continue
        results.append(f"{item.name} ({_format_size(item.stat().st_size)}) — {item.parent}")
        if len(results) >= max_results:
            break

    if not results:
        query = name or extension or "файлы"
        return f"«{query}» не найдено в {search_path.name}/"
    return f"Найдено {len(results)}:\n" + "\n".join(results)


def _get_largest_files(path: str, count: int) -> str:
    count = min(count, 50)
    search_path = _resolve_path(path)
    if not _is_safe_path(search_path):
        return f"Доступ запрещён: {search_path}"
    if not search_path.exists():
        return f"Путь не найден: {path}"

    files = []
    for item in search_path.rglob("*"):
        if item.is_file():
            try:
                files.append((item.stat().st_size, item))
            except Exception:
                continue
    files.sort(reverse=True)
    top = files[:count]
    if not top:
        return "Файлы не найдены."
    lines = [f"{len(top)} самых больших файлов в {search_path.name}/:"]
    for size, f in top:
        lines.append(f"  {_format_size(size):>10}  {f.name}  ({f.parent})")
    return "\n".join(lines)


def _get_disk_usage(path: str) -> str:
    target = _resolve_path(path)
    usage = shutil.disk_usage(target)
    pct = usage.used / usage.total * 100
    return (
        f"Диск ({target}):\n"
        f"  Всего:   {_format_size(usage.total)}\n"
        f"  Занято:  {_format_size(usage.used)} ({pct:.1f}%)\n"
        f"  Свободно: {_format_size(usage.free)}"
    )


def _get_file_info(path: str, name: str) -> str:
    base = _resolve_path(path)
    target = (base / name) if name else base
    if not _is_safe_path(target):
        return f"Доступ запрещён: {target}"
    if not target.exists():
        return f"Не найдено: {target.name}"
    stat = target.stat()
    info = {
        "Имя": target.name,
        "Тип": "Папка" if target.is_dir() else "Файл",
        "Размер": _format_size(stat.st_size),
        "Расположение": str(target.parent),
        "Создан": datetime.fromtimestamp(stat.st_ctime).strftime("%Y-%m-%d %H:%M"),
        "Изменён": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
        "Расширение": target.suffix or "—",
    }
    return "\n".join(f"{k}: {v}" for k, v in info.items())


FileAction = Literal[
    "list", "create_file", "create_folder", "delete", "move", "copy",
    "rename", "read", "write", "find", "largest", "disk_usage", "info",
]


@register_impl("file_manager")
@log_call("file_manager")
async def _file_manager(
    *,
    action: str,
    path: str = "desktop",
    name: str = "",
    destination: str = "",
    new_name: str = "",
    content: str = "",
    append: bool = False,
    extension: str = "",
    max_results: int = 20,
    count: int = 10,
    show_hidden: bool = False,
) -> dict:
    def _run() -> str:
        if action == "list":
            return _list_files(path, show_hidden)
        if action == "create_file":
            return _create_file(path, name, content)
        if action == "create_folder":
            return _create_folder(path, name)
        if action == "delete":
            return _delete_file(path, name)
        if action == "move":
            return _move_file(path, name, destination)
        if action == "copy":
            return _copy_file(path, name, destination)
        if action == "rename":
            return _rename_file(path, name, new_name)
        if action == "read":
            return _read_file(path, name, 4000)
        if action == "write":
            return _write_file(path, name, content, append)
        if action == "find":
            return _find_files(name, extension, path, min(max_results, 50))
        if action == "largest":
            return _get_largest_files(path, count)
        if action == "disk_usage":
            return _get_disk_usage(path)
        if action == "info":
            return _get_file_info(path, name)
        return f"Неизвестное действие file_manager: «{action}»."

    try:
        message = await asyncio.to_thread(_run)
        return {"status": "ok", "message": message}
    except Exception as exc:
        return {"status": "error", "message": f"Ошибка file_manager «{action}»: {exc}"}


@register_tool
@function_tool
async def file_manager(
    context: RunContext,
    action: FileAction,
    path: str = "desktop",
    name: str = "",
    destination: str = "",
    new_name: str = "",
    content: str = "",
    append: bool = False,
    extension: str = "",
    max_results: int = 20,
    count: int = 10,
    show_hidden: bool = False,
) -> str:
    """Browse, read, write, and organize files on any drive of the PC.
    System folders (Windows, Program Files, ProgramData, drive roots) are
    read-only here. Deletion always goes to the Recycle Bin, never permanent,
    and top-level user folders can't be deleted outright.

    Args:
        action: "list" a folder; "create_file"/"create_folder"; "delete"
            (to Recycle Bin); "move"/"copy" to `destination`; "rename" to
            `new_name`; "read"/"write" a text file's content; "find" files by
            `name`/`extension`; "largest" files by size; "disk_usage"; "info"
            about one file/folder.
        path: A shortcut ("desktop", "downloads", "documents", "pictures",
            "music", "videos", "home") or a real path. Default "desktop".
        name: File or folder name relative to `path` (omit to target `path` itself).
        destination: Target path, for "move"/"copy".
        new_name: New name, for "rename".
        content: Text content, for "create_file"/"write".
        append: Append instead of overwrite, for "write".
        extension: Filter like ".pdf", for "find".
        max_results: Cap for "find" (max 50).
        count: How many files to list, for "largest".
        show_hidden: Include dotfiles, for "list".
    """
    result = await _file_manager(
        action=action, path=path, name=name, destination=destination,
        new_name=new_name, content=content, append=append, extension=extension,
        max_results=max_results, count=count, show_hidden=show_hidden,
    )
    return result["message"]
