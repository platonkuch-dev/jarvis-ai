"""memes.py: inline [смех]/[мем: ...] tags and <think> stripping."""

from __future__ import annotations

import wave

import numpy as np
import pytest

import config
import memes


def _events(chunks: list[str]) -> list[tuple[str, str]]:
    f = memes.TagFilter()
    out: list[tuple[str, str]] = []
    for c in chunks:
        out += f.feed(c)
    out += f.flush()
    # merge adjacent text so assertions don't depend on chunking
    merged: list[tuple[str, str]] = []
    for kind, val in out:
        if merged and kind == "text" and merged[-1][0] == "text":
            merged[-1] = ("text", merged[-1][1] + val)
        else:
            merged.append((kind, val))
    return merged


def test_plain_text_passes_through():
    assert _events(["Привет, ", "бро."]) == [("text", "Привет, бро.")]


def test_laugh_and_meme_tags():
    assert _events(["Ну ты и гений [смех] сейчас [мем: бонк] ок"]) == [
        ("text", "Ну ты и гений "), ("tag", "laugh"), ("text", " сейчас "), ("tag", "бонк"), ("text", " ок"),
    ]


def test_tag_split_across_chunks():
    assert _events(["Ха ", "[м", "ем:", " вот это ", "поворот]", " да"]) == [
        ("text", "Ха "), ("tag", "вот это поворот"), ("text", " да"),
    ]


def test_non_tag_brackets_stay_text():
    assert _events(["массив [1, 2] и <b>"]) == [("text", "массив [1, 2] и <b>")]


def test_think_block_removed():
    assert _events(["<thi", "nk>\nдумаю...\n</th", "ink>\n\nОтвет"]) == [("text", "Ответ")]


def test_unclosed_bracket_flushed_as_text():
    assert _events(["скобка [без конца"]) == [("text", "скобка [без конца")]


def test_strip_tags():
    assert memes.strip_tags("Ну [смех] ты [мем: бонк] даёшь") == "Ну ты даёшь"


@pytest.fixture
def meme_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMES_DIR", tmp_path)
    (tmp_path / "смех").mkdir()
    return tmp_path


def _write_wav(path, seconds=0.5, rate=16000):
    t = np.arange(int(seconds * rate)) / rate
    data = (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())


def test_find_meme_fuzzy(meme_dir):
    _write_wav(meme_dir / "вот_это_поворот.wav")
    assert memes.find_meme("вот это поворот").name == "вот_это_поворот.wav"
    assert memes.find_meme("вот это повороот").name == "вот_это_поворот.wav"
    assert memes.find_meme("совсем другое") is None
    assert "вот это поворот" in memes.prompt_block()


async def test_clip_resampled_to_tts_rate(meme_dir):
    _write_wav(meme_dir / "бонк.wav", seconds=0.5, rate=16000)
    frames = await memes.clip_frames("бонк", 24000)
    assert frames and all(f.sample_rate == 24000 and f.num_channels == 1 for f in frames)
    total = sum(f.samples_per_channel for f in frames)
    assert abs(total - 12000) <= 24000 * 0.04  # ~0.5 s, padded to whole 20 ms frames


async def test_clip_truncated_to_max(meme_dir, monkeypatch):
    monkeypatch.setattr(config, "MEME_MAX_SECONDS", 1.0)
    _write_wav(meme_dir / "длинный.wav", seconds=3.0)
    frames = await memes.clip_frames("длинный", 24000)
    assert sum(f.samples_per_channel for f in frames) <= 24000 + 480


async def test_tts_splices_clip_between_segments(meme_dir, monkeypatch):
    from types import SimpleNamespace

    from livekit.agents import Agent

    _write_wav(meme_dir / "бонк.wav", seconds=0.1, rate=24000)
    spoken: list[str] = []

    async def fake_tts_node(agent, text, model_settings):
        parts = [t async for t in text]
        spoken.append("".join(parts))
        yield ("speech", "".join(parts))

    monkeypatch.setattr(Agent.default, "tts_node", staticmethod(fake_tts_node))
    agent = SimpleNamespace(session=SimpleNamespace(tts=SimpleNamespace(sample_rate=24000)))

    async def llm_text():
        for chunk in ["Ну ты ", "даёшь [м", "ем: бонк] давай ", "ещё [смех]"]:
            yield chunk

    out = [f async for f in memes.tts_with_sfx(agent, llm_text(), None)]
    # no laugh clips on disk -> [смех] is spoken inline as "ха-ха"
    assert spoken == ["Ну ты даёшь ", " давай ещё " + memes.LAUGH_FALLBACK_TEXT]
    kinds = ["speech" if isinstance(f, tuple) else "clip" for f in out]
    assert kinds[0] == "speech" and kinds[-1] == "speech"
    assert set(kinds[1:-1]) == {"clip"}


def test_emoji_removed():
    assert _events(["Ну ты 😂 даёшь 🙄"]) == [("text", "Ну ты  даёшь ")]


async def test_rickroll_opens_the_video_instead_of_a_clip(monkeypatch):
    opened = []
    monkeypatch.setattr(memes, "open_link_meme", opened.append)
    for tag in ("рикролл", "Рикрол", "rickroll"):
        assert await memes.clip_frames(tag, 24000) == []
    assert opened == [memes.LINK_MEMES["рикролл"][0]] * 3


def test_rickroll_is_offered_to_the_model():
    assert "рикролл" in memes.prompt_block()
    assert memes.find_link_meme("бонк") is None