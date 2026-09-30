"""lipsync_bridge: audio -> mouth-shape features, and the UDP hop to the HUD."""

from __future__ import annotations

import time

import numpy as np

import lipsync_bridge as lb

SR = 24000


def _tone(freq: float, amp: float, sec: float = 0.05) -> np.ndarray:
    t = np.arange(int(SR * sec)) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.int16)


def test_silence_closes_the_mouth():
    assert lb.analyze(np.zeros(1200, dtype=np.int16)) == (0.0, 0.0)


def test_loud_vowel_opens_wide_without_sibilance():
    level, sib = lb.analyze(_tone(220, 12000))
    assert level > 0.8 and sib < 0.1


def test_hiss_reads_as_sibilant():
    rng = np.random.default_rng(1)
    level, sib = lb.analyze((rng.standard_normal(1200) * 5000).astype(np.int16))
    assert level > 0.3 and sib > 0.8


def test_quiet_speech_still_moves_the_lips():
    level, _ = lb.analyze(_tone(200, 1500))
    assert 0.15 < level < 0.6


def test_packets_reach_the_hud_in_order():
    port = 48000 + int(time.time()) % 900
    rx, tx = lb.Receiver(port), lb.Sender(port)
    assert rx.ok
    for v in (0.1, 0.5, 0.9):
        tx.send(v, 0.2)
    time.sleep(0.05)
    got = rx.drain()
    assert [round(g[0], 2) for g in got] == [0.1, 0.5, 0.9]
    assert rx.drain() == []


def test_tap_wraps_console_playback_once():
    from livekit.agents.cli import _legacy

    original = _legacy.ConsoleAudioOutput.read_into
    try:
        assert lb.install_console_tap()
        wrapped = _legacy.ConsoleAudioOutput.read_into
        assert getattr(wrapped, "_jarvis_lipsync", False)
        assert lb.install_console_tap() and _legacy.ConsoleAudioOutput.read_into is wrapped
    finally:
        _legacy.ConsoleAudioOutput.read_into = original
