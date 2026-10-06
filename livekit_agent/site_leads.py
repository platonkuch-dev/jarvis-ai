"""New order requests from the portfolio site -> owner's Telegram.

The site (portfolio-site/, Cloudflare Pages + D1) can't reach this PC, so
proactive_monitor.py polls its /api/leads-feed every SITE_LEADS_POLL_S and
queues each new lead through notify.py, which delivers it from Jarvis's own
Telegram account like any other notification. The id of the last lead seen is
kept in data/site_leads.json, so leads that arrive while the PC is off are sent
once it is back, and nothing is sent twice. No LLM involved.
"""

from __future__ import annotations

import json
import logging

import httpx

import config
import notify
from atomic_io import atomic_write_text

logger = logging.getLogger("jarvis-voice-agent.site_leads")

STATE_FILE = config.DATA_DIR / "site_leads.json"

_BUDGET = {"<100": "до $100", "100-300": "$100–300", "300-1000": "$300–1000", "1000+": "$1000+"}


def enabled() -> bool:
    return bool(config.SITE_LEADS_URL and config.SITE_LEADS_KEY)


def _last_id() -> int:
    try:
        return int(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("last_id", 0))
    except (OSError, ValueError, TypeError):
        return 0


def _save_last_id(last_id: int) -> None:
    atomic_write_text(STATE_FILE, json.dumps({"last_id": last_id}))


def format_lead(lead: dict) -> str:
    budget = _BUDGET.get(lead.get("budget") or "", lead.get("budget") or "не указан")
    where = " · ".join(x for x in (lead.get("country"), lead.get("source")) if x)
    return (
        f"🆕 Заявка с сайта #{lead['id']}\n"
        f"👤 {lead.get('name', '')}\n"
        f"📬 {lead.get('contact', '')}\n"
        f"💰 {budget}" + (f" · {where}" if where else "") + "\n\n"
        f"{lead.get('message', '')}\n\n"
        f"Админка: {config.SITE_LEADS_URL}/admin.html"
    )


def poll() -> int:
    """Fetch leads newer than the last one seen and queue them. Returns how many were queued.
    Network errors are logged and retried on the next poll."""
    if not enabled():
        return 0
    after = _last_id()
    try:
        r = httpx.get(f"{config.SITE_LEADS_URL}/api/leads-feed", params={"after": after},
                      headers={"authorization": f"Bearer {config.SITE_LEADS_KEY}"}, timeout=15)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("site leads poll failed: %s", e)
        return 0
    leads = data.get("leads") or []
    for lead in leads:
        notify.notify_owner(format_lead(lead), kind="site_lead")
    # leads marked spam are skipped by the feed, so also move past them via last_id
    newest = max([after, int(data.get("last_id") or 0)] + [int(l["id"]) for l in leads])
    if len(leads) == 50:            # a full page: more may be waiting, continue from the last one sent
        newest = int(leads[-1]["id"])
    if newest != after:
        _save_last_id(newest)
    if leads:
        logger.info("queued %d new site lead(s)", len(leads))
    return len(leads)
