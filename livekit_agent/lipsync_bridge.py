"""Live mouth-shape feed from the voice worker to the HUD face (hud_bar.py).

hud_bridge.py's JSON file is polled every 150 ms -- far too coarse for lips.
This is the fast lane: worker.py (console mode) taps the exact samples the
sound card is being handed (livekit's ConsoleAudioOutput.read_into, called
on the audio thread) and fires one tiny UDP datagram per audio callback to
localhost. The HUD process reads them without blocking. Being the playback
buffer itself, not the TTS stream, it stays in sync with what's heard: TTS
is generated faster than real time and buffered, so tapping earlier would
move the lips ahead of the voice.

Each packet: level (0..1, loudness), sibilance (0..1, zero-crossing rate --
"s/ш/и" vs open "а/о"), monotonic send time. UDP to 127.0.0.1 never blocks
the audio thread, and nothing breaks if no HUD is listening.
"""

from __future__ import annotations

import logging
import socket
import struct
import time

import numpy as np

import config

logger = logging.getLogger("jarvis-voice-agent.lipsync")

PORT = config.LIPSYNC_PORT
_PACKET = struct.Struct("<ffd")          # level, sibilance, sent_at (time.monotonic)
_FULL_SCALE_RMS = 7000.0                 # int16 RMS that counts as a wide-open mouth


def analyze(samples: np.ndarray) -> tuple[float, float]:
    """(level, sibilance) of one block of int16 mono samples."""
    if samples.size == 0:
        return 0.0, 0.0
    x = samples.astype(np.float32)
    rms = float(np.sqrt(np.mean(x * x)))
    level = min(1.0, (rms / _FULL_SCALE_RMS) ** 0.7)   # perceptual-ish curve: quiet speech still moves the lips
    if rms < 120:
        return 0.0, 0.0
    signs = np.signbit(x)
    zcr = float(np.count_nonzero(signs[1:] != signs[:-1])) / max(1, x.size - 1)
    # voiced vowels at 24 kHz sit around 0.02-0.08 crossings/sample, fricatives well above 0.15
    sibilance = min(1.0, max(0.0, (zcr - 0.06) / 0.14))
    return level, sibilance


class Sender:
    def __init__(self, port: int = PORT) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        self._addr = ("127.0.0.1", port)

    def send(self, level: float, sibilance: float) -> None:
        try:
            self._sock.sendto(_PACKET.pack(level, sibilance, time.monotonic()), self._addr)
        except OSError:
            pass   # nobody listening / buffer full: dropping a frame is fine


class Receiver:
    def __init__(self, port: int = PORT) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        try:
            self._sock.bind(("127.0.0.1", port))
            self.ok = True
        except OSError:
            logger.warning("lipsync port %s is busy -- the face will animate without audio", port)
            self.ok = False

    def drain(self) -> list[tuple[float, float, float]]:
        """Every (level, sibilance, sent_at) received since the last call,
        oldest first. sent_at is time.monotonic() in the sender, which on
        Windows is the same system-wide tick clock in both processes."""
        out = []
        while self.ok:
            try:
                data = self._sock.recv(64)
            except (BlockingIOError, OSError):
                break
            if len(data) == _PACKET.size:
                out.append(_PACKET.unpack(data))
        return out


def install_console_tap() -> bool:
    """Wraps livekit's console playback so every block it plays is also
    analysed and sent to the HUD. Returns False if the console internals
    moved (then the face just animates from the status alone)."""
    try:
        from livekit.agents.cli import _legacy
    except Exception:
        return False
    cls = getattr(_legacy, "ConsoleAudioOutput", None)
    original = getattr(cls, "read_into", None)
    if original is None or getattr(original, "_jarvis_lipsync", False):
        return original is not None

    sender = Sender()

    def read_into(self, outdata, frames):
        original(self, outdata, frames)
        try:
            level, sib = analyze(outdata[:, 0] if outdata.ndim > 1 else outdata)
            sender.send(level, sib)
        except Exception:
            pass   # never let the face break the audio thread

    read_into._jarvis_lipsync = True
    cls.read_into = read_into
    logger.info("lipsync tap installed (udp %s)", PORT)
    return True
