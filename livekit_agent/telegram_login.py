"""One-time interactive Telegram login.

Run this directly in a terminal you can type into:

    python telegram_login.py              # Jarvis's own (non-bot) account
    python telegram_login.py --personal   # the owner's personal account

Jarvis's account: Telegram texts/calls TELEGRAM_PHONE (see .env) with a
verification code -- enter it when prompted. The session is saved to
config.TELEGRAM_SESSION_PATH and reused silently from then on.

--personal: asks for the owner's phone number, then the code Telegram sends
to the owner's own app (and the 2FA password if there is one). The session
goes to config.TELEGRAM_PERSONAL_SESSION_PATH; only chat_memory.py opens it,
to read (never send, never mark as read) the owner's recent chats into
Jarvis's shared memory. Deleting that file -- or ending the session under
Telegram > Settings > Devices -- switches it off again.
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv

load_dotenv()

import config
from telethon import TelegramClient


def main() -> None:
    if not config.TELEGRAM_API_ID or not config.TELEGRAM_API_HASH:
        print("Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в .env, затем запустите снова.")
        return

    personal = "--personal" in sys.argv
    session = config.TELEGRAM_PERSONAL_SESSION_PATH if personal else config.TELEGRAM_SESSION_PATH
    if personal:
        print("Вход в ВАШ личный Telegram: Джарвис будет только читать чаты (не писать и не отмечать прочитанными),")
        print("чтобы помнить, о чём вы переписываетесь. Отключить: Telegram > Настройки > Устройства.\n")
    client = TelegramClient(session, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH,
                            device_model="Jarvis AI (чтение чатов)" if personal else "Jarvis AI")
    client.start(phone=None if personal else (config.TELEGRAM_PHONE or None))
    me = client.get_me()
    print(f"Вход выполнен: {me.first_name} (@{me.username or 'без юзернейма'}), телефон {me.phone}")
    print(f"Сессия сохранена в {session}.session -- дальше вход не потребуется.")
    client.disconnect()
    if sys.stdin.isatty():
        input("\nГотово. Нажмите Enter, чтобы закрыть окно.")


if __name__ == "__main__":
    main()
