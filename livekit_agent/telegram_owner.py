"""Who owns Jarvis's Telegram account, and how ownership is claimed.

Ownership used to go to whoever messaged the account first -- anyone who
found the account before the real owner got full control of the PC. Now it
takes a one-time pairing code that is only visible in the local control
panel (127.0.0.1 + token): the owner sends that code to the account from
their own Telegram, and the code is burned the moment it's used.

Shared by telegram_bridge.py (claims), proactive_monitor.py (delivers
notifications to the owner) and panel.py (shows the code / owner).
"""

from __future__ import annotations

import json
import secrets
import time

import config

PAIR_CODE_FILE = config.DATA_DIR / "telegram_pair_code.txt"


def load_owner() -> dict | None:
    try:
        data = json.loads(config.TELEGRAM_OWNER_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("owner_id") else None


def load_owner_id() -> int | None:
    owner = load_owner()
    return int(owner["owner_id"]) if owner else None


def pairing_code() -> str:
    """The current code, creating one if there is none yet."""
    try:
        code = PAIR_CODE_FILE.read_text(encoding="utf-8").strip()
        if code:
            return code
    except OSError:
        pass
    code = f"{secrets.randbelow(10**6):06d}"
    PAIR_CODE_FILE.write_text(code, encoding="utf-8")
    return code


def try_claim(sender_id: int, sender_name: str, text: str) -> bool:
    """Makes `sender_id` the owner if nobody owns the account yet and `text`
    carries the current pairing code. Returns True on a successful claim."""
    if load_owner() is not None:
        return False
    try:
        code = PAIR_CODE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not code or code not in text.replace(" ", ""):
        return False
    config.TELEGRAM_OWNER_FILE.write_text(
        json.dumps({"owner_id": sender_id, "owner_name": sender_name,
                    "claimed": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False),
        encoding="utf-8",
    )
    PAIR_CODE_FILE.unlink(missing_ok=True)
    return True


def reset_owner() -> str:
    """Forget the current owner and issue a fresh pairing code."""
    config.TELEGRAM_OWNER_FILE.unlink(missing_ok=True)
    PAIR_CODE_FILE.unlink(missing_ok=True)
    return pairing_code()
