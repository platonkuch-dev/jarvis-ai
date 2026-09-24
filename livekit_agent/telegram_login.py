"""One-time interactive login for Jarvis's own (non-bot) Telegram account.

Run this directly in a terminal you can type into:

    python telegram_login.py

Telegram will text/call TELEGRAM_PHONE (see .env) with a verification code --
enter it when prompted. If the account has two-factor auth enabled, it'll
also ask for that password. On success, the session is saved to
config.TELEGRAM_SESSION_PATH -- tools/telegram_dm.py reuses it silently from
then on; this script never needs to run again unless that file is deleted.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import config
from telethon import TelegramClient


def main() -> None:
    if not config.TELEGRAM_API_ID or not config.TELEGRAM_API_HASH:
        print("Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в .env, затем запустите снова.")
        return

    client = TelegramClient(
        config.TELEGRAM_SESSION_PATH, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH
    )
    client.start(phone=config.TELEGRAM_PHONE or None)
    me = client.get_me()
    print(f"Вход выполнен: {me.first_name} (@{me.username or 'без юзернейма'}), телефон {me.phone}")
    print(f"Сессия сохранена в {config.TELEGRAM_SESSION_PATH}.session -- дальше вход не потребуется.")
    client.disconnect()


if __name__ == "__main__":
    main()
