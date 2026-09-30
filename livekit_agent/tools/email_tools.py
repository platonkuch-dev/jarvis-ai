"""E-mail over plain IMAP/SMTP with an app password (Gmail, Yandex, Mail.ru,
Outlook -- anything standard). No Google OAuth app to register.

check_email is read-only (opens the mailbox with readonly=True, so nothing
gets marked as read). send_email is two-step like other irreversible tools:
the first call reads the letter back, only confirm=True after the user's
spoken "да, подтверждаю" actually sends it.
"""

from __future__ import annotations

import asyncio
import email
import imaplib
import re
import smtplib
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parseaddr

from livekit.agents import RunContext, function_tool

import config
from tools._logging import log_call
from tools.registry import register_impl, register_tool

# Well-known hosts by address domain, so usually only address + app password are needed.
_HOSTS = {
    "gmail.com": ("imap.gmail.com", "smtp.gmail.com"),
    "googlemail.com": ("imap.gmail.com", "smtp.gmail.com"),
    "yandex.ru": ("imap.yandex.ru", "smtp.yandex.ru"),
    "ya.ru": ("imap.yandex.ru", "smtp.yandex.ru"),
    "yandex.com": ("imap.yandex.com", "smtp.yandex.com"),
    "mail.ru": ("imap.mail.ru", "smtp.mail.ru"),
    "bk.ru": ("imap.mail.ru", "smtp.mail.ru"),
    "inbox.ru": ("imap.mail.ru", "smtp.mail.ru"),
    "list.ru": ("imap.mail.ru", "smtp.mail.ru"),
    "outlook.com": ("outlook.office365.com", "smtp.office365.com"),
    "hotmail.com": ("outlook.office365.com", "smtp.office365.com"),
    "ukr.net": ("imap.ukr.net", "smtp.ukr.net"),
}


def _hosts() -> tuple[str, str]:
    domain = config.EMAIL_ADDRESS.rpartition("@")[2].lower()
    imap_default, smtp_default = _HOSTS.get(domain, (f"imap.{domain}", f"smtp.{domain}"))
    return config.EMAIL_IMAP_HOST or imap_default, config.EMAIL_SMTP_HOST or smtp_default


def _not_configured() -> dict | None:
    if config.EMAIL_ADDRESS and config.EMAIL_APP_PASSWORD:
        return None
    return {"status": "error", "message": (
        "Почта не настроена: укажите EMAIL_ADDRESS и EMAIL_APP_PASSWORD (пароль приложения, "
        "не основной пароль) в панели.")}


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _body_text(msg: email.message.Message, limit: int = 300) -> str:
    part = None
    if msg.is_multipart():
        for p in msg.walk():
            if p.get_content_type() == "text/plain" and "attachment" not in str(p.get("Content-Disposition", "")):
                part = p
                break
        if part is None:
            part = next((p for p in msg.walk() if p.get_content_type() == "text/html"), None)
    else:
        part = msg
    if part is None:
        return ""
    raw = part.get_payload(decode=True) or b""
    text = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _fetch(limit: int, unread_only: bool, query: str) -> tuple[int, list[dict]]:
    imap_host, _ = _hosts()
    with imaplib.IMAP4_SSL(imap_host, timeout=20) as box:
        box.login(config.EMAIL_ADDRESS, config.EMAIL_APP_PASSWORD)
        box.select("INBOX", readonly=True)
        _, unseen = box.search(None, "UNSEEN")
        unread_count = len(unseen[0].split()) if unseen and unseen[0] else 0
        if query:
            box.literal = query.encode("utf-8")
            _, found = box.search("UTF-8", "TEXT")
        else:
            _, found = box.search(None, "UNSEEN" if unread_only else "ALL")
        ids = (found[0].split() if found and found[0] else [])[-limit:]
        out = []
        for msg_id in reversed(ids):
            _, parts = box.fetch(msg_id, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if raw is None:
                continue
            msg = email.message_from_bytes(raw)
            name, addr = parseaddr(_decode(msg.get("From")))
            out.append({"from": name or addr, "address": addr, "subject": _decode(msg.get("Subject")) or "(без темы)",
                        "date": msg.get("Date", ""), "preview": _body_text(msg)})
        return unread_count, out


@register_impl("check_email")
@log_call("check_email")
async def _check_email(*, limit: int = 5, unread_only: bool = True, query: str = "") -> dict:
    if (err := _not_configured()) is not None:
        return err
    limit = max(1, min(int(limit or 5), 20))
    try:
        unread, letters = await asyncio.to_thread(_fetch, limit, unread_only, query.strip())
    except imaplib.IMAP4.error as exc:
        return {"status": "error", "message": f"Почтовый сервер отказал во входе: {exc}. Нужен пароль приложения."}
    except Exception as exc:
        return {"status": "error", "message": f"Не удалось проверить почту: {exc}"}
    if not letters:
        what = f"по запросу «{query}»" if query else ("непрочитанных" if unread_only else "")
        return {"status": "ok", "message": f"Писем {what} нет.".replace("  ", " "), "unread": unread}
    lines = [f"{i}. От {m['from']}: «{m['subject']}» — {m['preview'][:150]}" for i, m in enumerate(letters, 1)]
    return {"status": "ok", "unread": unread, "letters": letters,
            "message": f"Непрочитанных всего: {unread}.\n" + "\n".join(lines)}


@register_tool
@function_tool
async def check_email(context: RunContext, limit: int = 5, unread_only: bool = True, query: str = "") -> str:
    """Check the user's mailbox (read-only, nothing gets marked as read):
    newest unread letters with sender, subject and a short preview, or a search.

    Args:
        limit: How many letters (1-20), default 5.
        unread_only: True = only unread; False = latest letters regardless.
        query: Optional text to search in letters (sender, subject or body).
    """
    result = await _check_email(limit=limit, unread_only=unread_only, query=query)
    return result["message"]


def _send(to: str, subject: str, body: str) -> None:
    _, smtp_host = _hosts()
    msg = EmailMessage()
    msg["From"] = config.EMAIL_ADDRESS
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if config.EMAIL_SMTP_PORT == 465:
        with smtplib.SMTP_SSL(smtp_host, 465, timeout=20) as smtp:
            smtp.login(config.EMAIL_ADDRESS, config.EMAIL_APP_PASSWORD)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(smtp_host, config.EMAIL_SMTP_PORT, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(config.EMAIL_ADDRESS, config.EMAIL_APP_PASSWORD)
            smtp.send_message(msg)


@register_impl("send_email")
@log_call("send_email")
async def _send_email(*, to: str, subject: str, body: str, confirm: bool = False) -> dict:
    if (err := _not_configured()) is not None:
        return err
    to = to.strip()
    if not re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+", to):
        return {"status": "error", "message": f"«{to}» не похоже на адрес почты — уточните адрес."}
    if not confirm:
        return {"status": "needs_confirmation", "message": (
            f"Отправлю письмо на {to}, тема «{subject}»: «{body[:300]}». Скажите «да, подтверждаю».")}
    try:
        await asyncio.to_thread(_send, to, subject, body)
    except Exception as exc:
        return {"status": "error", "message": f"Не удалось отправить письмо: {exc}"}
    return {"status": "ok", "message": f"Письмо на {to} отправлено."}


@register_tool
@function_tool
async def send_email(context: RunContext, to: str, subject: str, body: str, confirm: bool = False) -> str:
    """Send an e-mail from the user's mailbox. Two-step: call with confirm=False
    to read the letter back, then confirm=True only after the user says
    «да, подтверждаю». Write the full text yourself. If the address is unknown,
    ask for it (or use check_email to find it) -- never guess one.

    Args:
        to: Recipient address, e.g. "ivan@example.com".
        subject: Subject line.
        body: Full letter text.
        confirm: True only after the user verbally confirmed.
    """
    result = await _send_email(to=to, subject=subject, body=body, confirm=confirm)
    return result["message"]
