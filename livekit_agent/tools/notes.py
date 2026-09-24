"""Notes, note search, local email drafts (never sent), and clipboard read."""

from __future__ import annotations

import asyncio
import re
import time

from livekit.agents import RunContext, function_tool

import config
from tools._logging import log_call
from tools.registry import register_impl, register_tool


def _safe_filename(name: str) -> str:
    name = name.strip() or time.strftime("note_%Y%m%d_%H%M%S")
    name = re.sub(r"[^\w\-. ]+", "_", name, flags=re.UNICODE)
    if not name.lower().endswith(".md"):
        name += ".md"
    return name


@register_impl("write_note")
@log_call("write_note")
async def _write_note(*, content: str, filename: str | None = None) -> dict:
    fname = _safe_filename(filename or "")
    path = config.NOTES_DIR / fname

    def _write() -> None:
        with open(path, "a", encoding="utf-8") as f:
            if path.stat().st_size > 0 if path.exists() else False:
                f.write("\n\n---\n\n")
            f.write(f"<!-- {time.strftime('%Y-%m-%d %H:%M:%S')} -->\n{content}\n")

    await asyncio.to_thread(_write)
    return {"status": "ok", "message": f"Заметка сохранена в {fname}.", "path": str(path)}


@register_tool
@function_tool
async def write_note(context: RunContext, content: str, filename: str | None = None) -> str:
    """Save a text note locally as markdown.

    Args:
        content: The text of the note to save.
        filename: Optional filename (without extension is fine). If omitted,
            a timestamped filename is used.
    """
    result = await _write_note(content=content, filename=filename)
    return result["message"]


@register_impl("search_notes")
@log_call("search_notes")
async def _search_notes(*, query: str) -> dict:
    q = query.strip().lower()
    if not q:
        return {"status": "error", "message": "Пустой запрос для поиска заметок."}

    def _scan() -> list[tuple[str, str]]:
        hits = []
        for path in config.NOTES_DIR.glob("*.md"):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if q in path.name.lower() or q in text.lower():
                idx = text.lower().find(q)
                snippet = text[max(0, idx - 40) : idx + 60].strip() if idx >= 0 else text[:80].strip()
                hits.append((path.name, snippet))
        return hits

    hits = await asyncio.to_thread(_scan)
    if not hits:
        return {"status": "not_found", "message": f"Заметок по запросу «{query}» не найдено."}

    listing = "; ".join(f"{name}: {snippet}" for name, snippet in hits[:5])
    return {"status": "ok", "message": f"Найдено {len(hits)}: {listing}", "hits": hits}


@register_tool
@function_tool
async def search_notes(context: RunContext, query: str) -> str:
    """Search saved notes by filename or content.

    Args:
        query: Text to search for across note filenames and contents.
    """
    result = await _search_notes(query=query)
    return result["message"]


@register_impl("draft_email")
@log_call("draft_email")
async def _draft_email(*, recipient_hint: str, topic: str, body: str) -> dict:
    fname = _safe_filename(f"draft_email_{recipient_hint}_{int(time.time())}")
    path = config.NOTES_DIR / fname
    content = f"Кому: {recipient_hint}\nТема: {topic}\n\n{body}\n"

    def _write() -> None:
        path.write_text(content, encoding="utf-8")

    await asyncio.to_thread(_write)
    return {
        "status": "ok",
        "message": f"Черновик письма для «{recipient_hint}» сохранён локально. Он НЕ отправлен.",
        "path": str(path),
    }


@register_tool
@function_tool
async def draft_email(context: RunContext, recipient_hint: str, topic: str, body: str) -> str:
    """Save a local email draft. This never sends anything -- it only writes a file.

    Compose the actual message text yourself and pass it as `body`; this tool
    just persists the draft for the user to review and send manually.

    Args:
        recipient_hint: Who the email is for, e.g. "Ивану" or "hr@company.com".
        topic: Short subject line.
        body: The full drafted email text.
    """
    result = await _draft_email(recipient_hint=recipient_hint, topic=topic, body=body)
    return result["message"]


@register_impl("read_clipboard")
@log_call("read_clipboard")
async def _read_clipboard() -> dict:
    try:
        import pyperclip
    except ImportError:
        return {"status": "error", "message": "Буфер обмена недоступен: не установлен pyperclip."}

    try:
        text = await asyncio.to_thread(pyperclip.paste)
    except Exception as exc:
        return {"status": "error", "message": f"Не удалось прочитать буфер обмена: {exc}"}

    if not text:
        return {"status": "empty", "message": "Буфер обмена пуст."}

    preview = text if len(text) <= 500 else text[:500] + "…"
    return {"status": "ok", "message": preview, "full_length": len(text)}


@register_tool
@function_tool
async def read_clipboard(context: RunContext) -> str:
    """Read the current text content of the system clipboard."""
    result = await _read_clipboard()
    return result["message"]
