"""Memes and laughs spliced into Jarvis's speech.

The LLM marks a spot in its reply with an inline tag -- `[смех]` or
`[мем: бонк]` -- and JarvisAgent.tts_node (worker.py) routes the reply
through `tts_with_sfx` below: the text before the tag is synthesized as
usual, then the clip's audio frames are yielded into the *same* output
stream, then synthesis continues. So a meme plays exactly where the model
put it, never on top of the voice, and it works the same in console mode
and in a LiveKit room -- it is just more frames of the reply.

Clips are plain files in config.MEMES_DIR (data/memes/): the file name
without its extension is the meme's name. An optional data/memes/memes.json
maps names to a short "when it fits" hint for the model:

    {"бонк": "когда пользователь сморозил глупость"}

Laugh clips go in data/memes/смех/; one is picked at random per [смех] tag
(with no clips there, the tag is spoken as a short "ха-ха" instead).

`TagFilter` also drops `<think>...</think>` blocks, which Qwen3 on Ollama
can leave in the content stream even with thinking disabled.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import random
import re
from collections import deque
from collections.abc import AsyncIterable, AsyncIterator
from pathlib import Path

import numpy as np
from livekit import rtc

import config

logger = logging.getLogger("jarvis-voice-agent.memes")

AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".m4a", ".opus", ".flac"}
LAUGH_DIR_NAME = "смех"
LAUGH_FALLBACK_TEXT = "Ха-ха! "
_FRAME_MS = 20
_MAX_TAG_LEN = 64
# ~ the loudness of typical TTS speech in int16 units
_TARGET_RMS = 4500.0

# [смех] / [laugh] / [мем: имя] / [meme: name]
_TAG_RE = re.compile(r"^\[\s*(смех|ржач|laugh|мем|meme)\s*(?::\s*([^\]]{1,60}?))?\s*\]$", re.IGNORECASE)
_STRIP_RE = re.compile(r"\[\s*(?:смех|ржач|laugh|мем|meme)\s*(?::[^\]]{0,60})?\]|<think>.*?</think>",
                       re.IGNORECASE | re.DOTALL)

_THINK_OPEN, _THINK_CLOSE = "<think>", "</think>"
# Emoji and pictographs: TTS engines either read them out ("smiling face")
# or choke on them, and the prompt already asks for none.
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]+")


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------

def _audio_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTS)


def _descriptions() -> dict[str, str]:
    try:
        raw = json.loads((config.MEMES_DIR / "memes.json").read_text(encoding="utf-8"))
        return {str(k).strip().lower(): str(v).strip() for k, v in raw.items()}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning("could not read memes.json", exc_info=True)
        return {}


def list_memes() -> dict[str, Path]:
    """name (lowercase, underscores as spaces) -> file."""
    return {p.stem.replace("_", " ").strip().lower(): p for p in _audio_files(config.MEMES_DIR)}


def laugh_files() -> list[Path]:
    return _audio_files(config.MEMES_DIR / LAUGH_DIR_NAME)


# Memes that are an action, not a sound clip: the tag opens a link in the
# default browser. The rickroll is the real thing -- the official video --
# rather than a copy of the song bundled with the app.
LINK_MEMES: dict[str, tuple[str, str]] = {
    "рикролл": ("https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "розыгрыш: сделай вид, что открываешь что-то полезное (ответ, ссылку, «секретный "
                "файл»), и вместо этого вставь тег — у владельца откроется Rick Astley"),
}


def find_link_meme(name: str) -> str | None:
    key = name.replace("_", " ").strip().lower()
    if key in LINK_MEMES:
        return LINK_MEMES[key][0]
    close = difflib.get_close_matches(key, list(LINK_MEMES), n=1, cutoff=0.7)
    if close:
        return LINK_MEMES[close[0]][0]
    # "rickroll", "рикрол", "рикролл!" ...
    if "rick" in key or "рикрол" in key:
        return LINK_MEMES["рикролл"][0]
    return None


def open_link_meme(url: str) -> None:
    import webbrowser

    try:
        webbrowser.open(url, new=2)
        logger.info("link meme opened: %s", url)
    except Exception:
        logger.warning("could not open link meme %s", url, exc_info=True)


def find_meme(name: str) -> Path | None:
    memes = list_memes()
    key = name.replace("_", " ").strip().lower()
    if key in memes:
        return memes[key]
    # The model sometimes paraphrases a name slightly ("бонк!" / "бонька").
    close = difflib.get_close_matches(key, list(memes), n=1, cutoff=0.6)
    if close:
        return memes[close[0]]
    contains = [n for n in memes if key in n or n in key]
    return memes[contains[0]] if contains else None


def prompt_block() -> str:
    """The part of the system prompt telling the model which tags it can use."""
    memes = list_memes()
    hints = _descriptions()
    lines = [
        "СМЕХ И МЕМЫ:",
        "- Чтобы засмеяться вслух, вставь в ответ тег [смех] (ровно так, в квадратных скобках) —",
        "  на этом месте прозвучит настоящий смех. Не пиши «ха-ха» словами, используй тег.",
    ]
    if memes or LINK_MEMES:
        lines += [
            "- Чтобы включить звуковой мем, вставь тег [мем: название] — он прозвучит в этом месте",
            "  ответа. Используй только названия из списка ниже, другие не сработают:",
        ]
        for name in memes:
            hint = hints.get(name)
            lines.append(f"  • {name}" + (f" — {hint}" if hint else ""))
        for name, (_url, hint) in LINK_MEMES.items():
            lines.append(f"  • {name} — {hint}. Не чаще раза за разговор, и не когда владелец спешит.")
        lines += [
            "- Мем — это приправа, а не каждый ответ: включай, когда он реально в тему (пользователь",
            "  облажался, что-то удалось, неожиданный поворот, он сам просит мем). Примерно один мем",
            "  на несколько реплик, не больше одного за ответ. Если просят «включи мем» без уточнения —",
            "  выбери самый подходящий по ситуации.",
        ]
    else:
        lines.append("- Звуковых мемов пока нет. Если просят мем — скажи, что их надо закинуть в папку data/memes.")
    return "\n".join(lines)


def strip_tags(text: str) -> str:
    """Tag-free text for the HUD/transcript (the chat history keeps the tags,
    so the model sees what it did)."""
    return re.sub(r"[ \t]{2,}", " ", _STRIP_RE.sub("", text)).strip()


# ---------------------------------------------------------------------------
# Streaming tag parser
# ---------------------------------------------------------------------------

class TagFilter:
    """Incremental parser: feed LLM text chunks, get ("text", s) and
    ("tag", "laugh" | meme-name) events. Tags may be split across chunks."""

    def __init__(self) -> None:
        self._buf = ""
        self._in_think = False

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        self._buf += chunk
        return self._drain(final=False)

    def flush(self) -> list[tuple[str, str]]:
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        while self._buf:
            if self._in_think:
                end = self._buf.find(_THINK_CLOSE)
                if end < 0:
                    # keep a possible partial "</think>" for the next chunk
                    self._buf = "" if final else self._buf[-(len(_THINK_CLOSE) - 1):]
                    break
                self._buf = self._buf[end + len(_THINK_CLOSE):].lstrip()
                self._in_think = False
                continue

            self._buf = _EMOJI_RE.sub("", self._buf)
            if not self._buf:
                break
            special = min((i for i in (self._buf.find("["), self._buf.find("<")) if i >= 0), default=-1)
            if special < 0:
                out.append(("text", self._buf))
                self._buf = ""
                break
            if special > 0:
                out.append(("text", self._buf[:special]))
                self._buf = self._buf[special:]

            if self._buf.startswith("<"):
                if self._buf.startswith(_THINK_OPEN):
                    self._buf = self._buf[len(_THINK_OPEN):]
                    self._in_think = True
                    continue
                if _THINK_OPEN.startswith(self._buf) and not final:
                    break  # maybe "<thi" -- wait for more
                out.append(("text", "<"))
                self._buf = self._buf[1:]
                continue

            # starts with "["
            close = self._buf.find("]")
            if close < 0:
                if len(self._buf) < _MAX_TAG_LEN and not final:
                    break  # tag may still be arriving
                out.append(("text", "["))
                self._buf = self._buf[1:]
                continue
            candidate = self._buf[: close + 1]
            m = _TAG_RE.match(candidate)
            if m is None:
                out.append(("text", "["))
                self._buf = self._buf[1:]
                continue
            kind, name = m.group(1).lower(), (m.group(2) or "").strip()
            if kind in ("смех", "ржач", "laugh"):
                out.append(("tag", "laugh"))
            elif name:
                out.append(("tag", name))
            self._buf = self._buf[close + 1:]
        return out


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

_clip_cache: dict[tuple[str, float, int], list[rtc.AudioFrame]] = {}


def _load_clip(path: Path, sample_rate: int) -> list[rtc.AudioFrame]:
    key = (str(path), path.stat().st_mtime, sample_rate)
    cached = _clip_cache.get(key)
    if cached is not None:
        return cached

    import av  # PyAV ships with livekit-agents

    chunks: list[np.ndarray] = []
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=sample_rate)
        for frame in container.decode(stream):
            for rf in resampler.resample(frame):
                chunks.append(rf.to_ndarray().reshape(-1))
        for rf in resampler.resample(None):
            chunks.append(rf.to_ndarray().reshape(-1))
    if not chunks:
        return []
    pcm = np.concatenate(chunks).astype(np.float32)

    max_len = int(config.MEME_MAX_SECONDS * sample_rate)
    if len(pcm) > max_len:
        pcm = pcm[:max_len]
        fade = min(len(pcm), int(0.15 * sample_rate))
        pcm[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)

    # Loudness-match roughly to speech by RMS, not peak: peak-normalizing
    # made dense clips (airhorn, buzzer) ~8x louder than a quiet chuckle.
    # The peak cap keeps sparse clips from clipping when boosted.
    peak = float(np.abs(pcm).max()) or 1.0
    rms = float(np.sqrt(np.mean(pcm ** 2))) or 1.0
    gain = min(_TARGET_RMS / rms, 0.95 * 32767 / peak)
    pcm = pcm * gain * config.MEME_VOLUME
    pcm16 = np.clip(pcm, -32768, 32767).astype(np.int16)

    per_frame = sample_rate * _FRAME_MS // 1000
    frames = []
    for i in range(0, len(pcm16), per_frame):
        part = pcm16[i:i + per_frame]
        if len(part) < per_frame:
            part = np.pad(part, (0, per_frame - len(part)))
        frames.append(rtc.AudioFrame(data=part.tobytes(), sample_rate=sample_rate,
                                     num_channels=1, samples_per_channel=per_frame))
    _clip_cache[key] = frames
    return frames


async def clip_frames(tag: str, sample_rate: int) -> list[rtc.AudioFrame]:
    if tag == "laugh":
        files = laugh_files()
        path = random.choice(files) if files else None
    else:
        # Link memes first: find_meme's fuzzy match would otherwise happily
        # turn "рикролл" into the closest-sounding clip.
        url = find_link_meme(tag)
        if url:
            await asyncio.to_thread(open_link_meme, url)
            return []
        path = find_meme(tag)
    if path is None:
        logger.info("meme %r not found in %s", tag, config.MEMES_DIR)
        return []
    try:
        frames = await asyncio.to_thread(_load_clip, path, sample_rate)
        logger.info("playing meme %s", path.name)
        return frames
    except Exception:
        logger.warning("could not decode meme %s", path, exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Pipeline nodes
# ---------------------------------------------------------------------------

async def tts_with_sfx(agent, text: AsyncIterable[str], model_settings) -> AsyncIterator[rtc.AudioFrame]:
    """Drop-in body for Agent.tts_node: speech with clips spliced in at tags."""
    from livekit.agents import Agent

    upstream = text.__aiter__()
    filt = TagFilter()
    pending: deque[tuple[str, str]] = deque()
    exhausted = False
    has_laughs = bool(laugh_files())

    async def next_event() -> tuple[str, str] | None:
        nonlocal exhausted
        while not pending:
            if exhausted:
                return None
            try:
                chunk = await upstream.__anext__()
            except StopAsyncIteration:
                exhausted = True
                pending.extend(filt.flush())
                continue
            pending.extend(filt.feed(chunk))
        ev = pending.popleft()
        if ev == ("tag", "laugh") and not has_laughs:
            return ("text", LAUGH_FALLBACK_TEXT)
        return ev

    sample_rate = agent.session.tts.sample_rate
    ev = await next_event()
    while ev is not None:
        kind, value = ev
        if kind == "tag":
            for frame in await clip_frames(value, sample_rate):
                yield frame
            ev = await next_event()
            continue
        if not value.strip():
            ev = await next_event()
            continue

        # A run of text up to the next tag becomes one TTS stream, still
        # streamed chunk by chunk so the first words start playing early.
        state: dict[str, tuple[str, str] | None] = {"next": None}

        async def segment(first: str = value, state: dict = state) -> AsyncIterator[str]:
            yield first
            while True:
                e = await next_event()
                if e is None or e[0] == "tag":
                    state["next"] = e
                    return
                yield e[1]

        async for frame in Agent.default.tts_node(agent, segment(), model_settings):
            yield frame
        ev = state["next"]


async def strip_transcription(text: AsyncIterable) -> AsyncIterator[str]:
    """Drop-in body for Agent.transcription_node: same text, minus the tags."""
    filt = TagFilter()
    async for delta in text:
        for kind, value in filt.feed(str(delta)):
            if kind == "text":
                yield value
    for kind, value in filt.flush():
        if kind == "text":
            yield value
