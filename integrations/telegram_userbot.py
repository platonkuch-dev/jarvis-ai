"""
integrations/telegram_userbot.py — JARVIS Telegram userbot bridge.

Connects to a *personal* Telegram account (not a bot) via the official
MTProto API (Telethon), so JARVIS can:

  - receive incoming messages sent to that account and, if the sender is
    the authorized user, feed them into the normal JARVIS command pipeline
  - send messages out from that account (used by the send_message tool)

This module never performs the login itself — it only opens an *existing*
saved session. Run  integrations/telegram_login.py  once, yourself, in a
terminal to create that session (phone number + login code + optional 2FA,
typed directly by you — JARVIS/Claude never sees them).

Config keys (stored in the same api_keys.json JARVIS already uses):
  telegram_api_id            int, from https://my.telegram.org
  telegram_api_hash          str, from https://my.telegram.org
  telegram_authorized_id     str, numeric Telegram user id allowed to command JARVIS
  telegram_authorized_username  str, username (no @) allowed to command JARVIS
(at least one of telegram_authorized_id / telegram_authorized_username must be set)
"""
import asyncio

from core.path_utils import get_user_data_dir
from core.config import GEMINI_VOICE_NAME
from core.runtime_config import get_config as _load_config

try:
    from telethon import TelegramClient, events
    _TELETHON = True
except ImportError:
    _TELETHON = False


def _session_path() -> str:
    return str(get_user_data_dir() / "telegram_userbot")


async def synthesize_speech(text: str) -> bytes | None:
    """Text -> raw 16-bit mono 24kHz PCM via Gemini's TTS model. Independent of
    the Live conversational session, so it can run for an arbitrary recipient
    at any time, not just as a reply in an active turn."""
    cfg     = _load_config()
    api_key = cfg.get("gemini_api_key")
    if not api_key or not text:
        return None
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=api_key, http_options={"api_version": "v1beta"})
        resp = await client.aio.models.generate_content(
            model="gemini-2.5-flash-preview-tts",
            contents=text,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=GEMINI_VOICE_NAME)
                    )
                ),
            ),
        )
        return resp.candidates[0].content.parts[0].inline_data.data
    except Exception as e:
        print(f"[Telegram] TTS synthesis failed: {e}")
        return None


async def _transcribe_voice(audio_bytes: bytes) -> str:
    """Transcribe a Telegram voice note (OGG/Opus) via Gemini — no local STT needed."""
    cfg     = _load_config()
    api_key = cfg.get("gemini_api_key")
    if not api_key or not audio_bytes:
        return ""
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=api_key, http_options={"api_version": "v1beta"})
        resp = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg"),
                "Transcribe this voice message exactly as spoken, in its original language. "
                "Output only the transcription, nothing else.",
            ],
        )
        return (resp.text or "").strip()
    except Exception as e:
        print(f"[Telegram] Voice transcription failed: {e}")
        return ""


async def resolve_contact(client, name: str):
    """
    Resolve a free-form recipient (e.g. "Катя", "мама") into a Telegram user
    entity, so commands like "напиши Кате что я опоздаю" work without an
    exact @username/phone/id.

    Tries, in order:
      1. Direct resolution via get_entity — for things that already look
         like a username ("@x"), phone ("+7..."), or numeric id.
      2. Exact (case-insensitive) match against saved contacts' and recent
         chats' display names, or just their first name.
      3. Unique substring match.
      4. Unique fuzzy match (typo tolerance).

    Returns (entity, ambiguous_names):
      - entity is the matched user, or None if nothing matched.
      - ambiguous_names is non-empty only when several plausible contacts
        were found and the caller should ask the user to pick one.
    """
    raw = (name or "").strip()
    if not raw:
        return None, []

    looks_direct = raw.startswith("@") or raw.startswith("+") or raw.lstrip("-").isdigit()
    if looks_direct:
        try:
            return await client.get_entity(raw), []
        except Exception:
            pass

    candidates: dict = {}  # entity id -> (display_name, entity)

    try:
        from telethon.tl.functions.contacts import GetContactsRequest
        result = await client(GetContactsRequest(hash=0))
        for user in getattr(result, "users", []):
            full = f"{user.first_name or ''} {user.last_name or ''}".strip()
            if full:
                candidates[user.id] = (full, user)
    except Exception:
        pass

    try:
        async for dialog in client.iter_dialogs(limit=300):
            if dialog.is_user and dialog.entity and dialog.entity.id not in candidates:
                full = (dialog.name or "").strip()
                if full:
                    candidates[dialog.entity.id] = (full, dialog.entity)
    except Exception:
        pass

    if not candidates:
        if not looks_direct:
            try:
                return await client.get_entity(raw), []
            except Exception:
                pass
        return None, []

    name_lower = raw.lower()

    exact = [(full, ent) for full, ent in candidates.values()
             if full.lower() == name_lower or full.lower().split(" ")[0] == name_lower]
    if len(exact) == 1:
        return exact[0][1], []
    if len(exact) > 1:
        return None, sorted({full for full, _ in exact})

    partial = [(full, ent) for full, ent in candidates.values() if name_lower in full.lower()]
    if len(partial) == 1:
        return partial[0][1], []
    if len(partial) > 1:
        return None, sorted({full for full, _ in partial})

    import difflib
    all_names   = [full for full, _ in candidates.values()]
    close_lower = difflib.get_close_matches(name_lower, [n.lower() for n in all_names], n=3, cutoff=0.6)
    if close_lower:
        matched = [(full, ent) for full, ent in candidates.values() if full.lower() in close_lower]
        if len(matched) == 1:
            return matched[0][1], []
        return None, sorted({full for full, _ in matched})

    if not looks_direct:
        try:
            return await client.get_entity(raw), []
        except Exception:
            pass

    return None, []


class TelegramUserbot:
    """Wraps a Telethon client. Lives on the same asyncio loop as JarvisLive."""

    def __init__(self):
        self.client = None
        self._command_queue: asyncio.Queue = asyncio.Queue()
        self._reply_with_voice: set = set()   # chat_ids whose last message was a voice note

    def should_reply_with_voice(self, chat_id) -> bool:
        """One-shot check: True if the triggering message for this chat was a voice note."""
        if chat_id in self._reply_with_voice:
            self._reply_with_voice.discard(chat_id)
            return True
        return False

    def _is_authorized_sender(self, sender) -> bool:
        cfg           = _load_config()
        allowed_id    = str(cfg.get("telegram_authorized_id", "")).strip()
        allowed_user  = str(cfg.get("telegram_authorized_username", "")).strip().lstrip("@").lower()
        if not allowed_id and not allowed_user:
            return False  # nobody configured yet — refuse everyone, fail safe
        sender_id   = str(getattr(sender, "id", ""))
        sender_user = (getattr(sender, "username", "") or "").lower()
        return (allowed_id and sender_id == allowed_id) or (allowed_user and sender_user == allowed_user)

    async def run(self) -> None:
        """Connect using an existing saved session. Never prompts interactively."""
        if not _TELETHON:
            print("[Telegram] Userbot disabled — run: pip install telethon")
            return

        cfg      = _load_config()
        api_id   = cfg.get("telegram_api_id")
        api_hash = cfg.get("telegram_api_hash")
        if not api_id or not api_hash:
            print("[Telegram] Userbot disabled — no telegram_api_id/telegram_api_hash configured. "
                  "Run: python integrations/telegram_login.py")
            return

        backoff = 3
        while True:
            try:
                self.client = TelegramClient(_session_path(), int(api_id), api_hash)
                await self.client.connect()

                if not await self.client.is_user_authorized():
                    print("[Telegram] Not logged in yet. Run: python integrations/telegram_login.py")
                    await self.client.disconnect()
                    self.client = None
                    return

                me = await self.client.get_me()
                print(f"[Telegram] Userbot connected as {me.first_name} (@{me.username or me.id}).")

                @self.client.on(events.NewMessage(incoming=True))
                async def _handler(event):
                    try:
                        sender = await event.get_sender()
                        if sender is None or getattr(sender, "bot", False):
                            return
                        if not self._is_authorized_sender(sender):
                            return

                        text = (event.raw_text or "").strip()

                        if not text and event.message and event.message.voice:
                            audio_bytes = await event.message.download_media(bytes)
                            text = await _transcribe_voice(audio_bytes)
                            if text:
                                print(f"[Telegram] 🎤 Voice transcribed: {text[:80]}")
                                self._reply_with_voice.add(event.chat_id)

                        if not text:
                            return
                        await self._command_queue.put((text, event.chat_id))
                    except Exception as e:
                        print(f"[Telegram] Handler error: {e}")

                await self.client.run_until_disconnected()
                # run_until_disconnected() returned → connection dropped, retry below
                print("[Telegram] Disconnected — reconnecting...")

            except Exception as e:
                print(f"[Telegram] Connection error: {e}")

            self.client = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def send_reply(self, chat_id, text: str) -> None:
        if not self.client or not text:
            return
        try:
            await self.client.send_message(chat_id, text)
        except Exception as e:
            print(f"[Telegram] send_reply failed: {e}")

    async def send_voice(self, chat_id, pcm_bytes: bytes, sample_rate: int = 24000) -> None:
        """Send raw 16-bit mono PCM (as already produced by JARVIS's Gemini Live
        voice output) as a playable audio message. Wrapped as WAV — Telegram
        clients play it inline; it just won't render as the round voice-note
        bubble, which requires an OGG/Opus container we don't encode here."""
        if not self.client or not pcm_bytes:
            return
        try:
            import io
            import wave
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(pcm_bytes)
            buf.seek(0)
            buf.name = "reply.wav"
            await self.client.send_file(chat_id, buf)
        except Exception as e:
            print(f"[Telegram] send_voice failed: {e}")

    async def send_photo(self, chat_id, image_bytes: bytes, caption: str = "", filename: str = "screenshot.jpg") -> None:
        if not self.client or not image_bytes:
            return
        try:
            import io
            buf = io.BytesIO(image_bytes)
            buf.name = filename
            # force_document avoids Telegram's photo pipeline re-compressing the
            # image (which would undo a deliberately full-quality screenshot)
            await self.client.send_file(chat_id, buf, caption=caption, force_document=True)
        except Exception as e:
            print(f"[Telegram] send_photo failed: {e}")

    async def send_voice_to(self, receiver: str, text: str) -> str:
        """
        Synthesizes `text` to speech and sends it as a voice message to
        `receiver` (username, phone, numeric id, or free-form contact name).
        """
        if not self.client:
            return "Telegram userbot is not connected."
        if not receiver:
            return "Please specify a recipient."
        if not text:
            return "Please specify what the voice message should say."
        try:
            entity, ambiguous = await resolve_contact(self.client, receiver)
        except Exception as e:
            return f"Could not look up '{receiver}': {e}"
        if entity is None:
            if ambiguous:
                return (
                    f"I found more than one contact matching '{receiver}': "
                    f"{', '.join(ambiguous)}. Tell me which one, or use their exact @username."
                )
            return f"I couldn't find a Telegram contact matching '{receiver}'."

        pcm = await synthesize_speech(text)
        if not pcm:
            return "Could not synthesize speech for the voice message."
        try:
            await self.send_voice(entity, pcm)
            display = getattr(entity, "first_name", None) or receiver
            return f"Voice message sent to {display} via Telegram."
        except Exception as e:
            return f"Could not send voice message to {receiver}: {e}"

    async def send_to(self, receiver: str, text: str) -> str:
        """
        Used by the send_message tool. receiver can be a username, phone,
        numeric id, or a free-form contact name (e.g. "Катя") — resolved
        against saved contacts and recent chats.
        """
        if not self.client:
            return "Telegram userbot is not connected."
        if not receiver:
            return "Please specify a recipient."
        try:
            entity, ambiguous = await resolve_contact(self.client, receiver)
        except Exception as e:
            return f"Could not look up '{receiver}': {e}"
        if entity is None:
            if ambiguous:
                return (
                    f"I found more than one contact matching '{receiver}': "
                    f"{', '.join(ambiguous)}. Tell me which one, or use their exact @username."
                )
            return f"I couldn't find a Telegram contact matching '{receiver}'."
        try:
            await self.client.send_message(entity, text)
            display = getattr(entity, "first_name", None) or receiver
            return f"Message sent to {display} via Telegram."
        except Exception as e:
            return f"Could not send Telegram message to {receiver}: {e}"
