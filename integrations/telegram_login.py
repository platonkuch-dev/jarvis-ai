"""
integrations/telegram_login.py — one-time interactive login for the JARVIS
Telegram userbot.

RUN THIS YOURSELF in a terminal, on the SECOND Telegram account:

    python integrations/telegram_login.py

You will be asked for:
  1. api_id / api_hash        — from https://my.telegram.org (API development tools)
  2. the phone number of the second account
  3. the login code Telegram sends to that account
  4. your 2FA password, only if you have Two-Step Verification enabled

Everything here is typed by YOU directly into this terminal. Nothing is
sent to or seen by Claude/JARVIS — this script just talks to Telegram's
servers and saves a local session file so the main JARVIS process can use
it later without asking you to log in again.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.path_utils import get_config_path, get_user_data_dir  # noqa: E402
from core.runtime_config import get_config as _load_config, invalidate_config_cache  # noqa: E402

try:
    from telethon import TelegramClient
except ImportError:
    print("Telethon is not installed. Run:  pip install telethon")
    sys.exit(1)


def _save_config(cfg: dict) -> None:
    get_config_path().write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    invalidate_config_cache()


def main() -> None:
    # get_config()/_load_config() returns the shared process-wide cache —
    # copy before mutating so we don't corrupt it ahead of actually saving.
    cfg = dict(_load_config())

    print("=== JARVIS — Telegram userbot login ===\n")

    api_id = str(cfg.get("telegram_api_id") or "").strip() or input("api_id (from my.telegram.org): ").strip()
    api_hash = str(cfg.get("telegram_api_hash") or "").strip() or input("api_hash (from my.telegram.org): ").strip()
    phone = str(cfg.get("telegram_phone") or "").strip() or input(
        "Phone number of the SECOND Telegram account (e.g. +79991234567): "
    ).strip()

    cfg["telegram_api_id"]   = api_id
    cfg["telegram_api_hash"] = api_hash
    cfg["telegram_phone"]    = phone
    _save_config(cfg)

    session_path = str(get_user_data_dir() / "telegram_userbot")
    client = TelegramClient(session_path, int(api_id), api_hash)

    async def _run():
        await client.start(phone=phone)  # prompts for code / 2FA password itself, right here
        me = await client.get_me()
        print(f"\nLogged in as: {me.first_name} {me.last_name or ''}  (@{me.username})  id={me.id}")
        print(f"Session saved to: {session_path}.session\n")

        cfg2 = dict(_load_config())
        if not cfg2.get("telegram_authorized_id") and not cfg2.get("telegram_authorized_username"):
            print(
                "Now: who is ALLOWED to command JARVIS through this account?\n"
                "This should be YOUR MAIN Telegram account — the one you'll message this\n"
                "second account from. Give either its @username or numeric user id."
            )
            allowed_username = input("Your main account's @username (leave blank to use numeric id instead): ").strip().lstrip("@")
            allowed_id = ""
            if not allowed_username:
                allowed_id = input("Your main account's numeric user id: ").strip()
            cfg2["telegram_authorized_username"] = allowed_username
            cfg2["telegram_authorized_id"]       = allowed_id
            _save_config(cfg2)
            print("Saved authorization setting.")

        print("\nDone. You can close this window and start JARVIS normally —")
        print("it will pick up this session automatically.")
        await client.disconnect()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
