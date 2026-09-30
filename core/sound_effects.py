"""Small original synthesized UI sounds for JARVIS state changes."""
from __future__ import annotations

import math
import threading

import numpy as np


SAMPLE_RATE = 48_000
def startup_sequence() -> np.ndarray:
    """A long, low-only startup bass swell with a fading spatial echo tail."""
    duration = 5.4
    time_axis = np.arange(int(SAMPLE_RATE * duration), dtype=np.float32) / SAMPLE_RATE
    core_duration = 2.15
    core_samples = int(SAMPLE_RATE * core_duration)
    core_time = time_axis[:core_samples]
    progress = core_time / core_duration
    attack = np.minimum(1.0, core_time / 0.55)
    release = np.minimum(1.0, (core_duration - core_time) / 0.7)
    envelope = attack * release
    core = np.zeros(core_samples, dtype=np.float32)
    width = np.zeros(core_samples, dtype=np.float32)
    layers = ((34.0, 53.0, 0.34), (47.0, 68.0, 0.25), (61.0, 91.0, 0.16), (78.0, 118.0, 0.07))
    for index, (start_hz, end_hz, volume) in enumerate(layers):
        frequency = start_hz + (end_hz - start_hz) * progress ** (1.55 + index * 0.22)
        phase = 2.0 * np.pi * np.cumsum(frequency) / SAMPLE_RATE
        layer = np.sin(phase + index * 0.73) * volume * envelope
        core += layer
        width += layer * math.sin(index * 1.7 + 0.5)

    # Successive low-frequency echoes create a long decay without any bright layer.
    left = np.zeros_like(time_axis)
    right = np.zeros_like(time_axis)
    for delay, gain, pan in ((0.0, 1.0, 0.24), (0.46, 0.48, -0.18), (1.05, 0.26, 0.16), (1.78, 0.13, -0.12), (2.58, 0.06, 0.08)):
        start = int(delay * SAMPLE_RATE)
        end = min(len(time_axis), start + core_samples)
        length = end - start
        left[start:end] += (core[:length] + width[:length] * pan) * gain
        right[start:end] += (core[:length] - width[:length] * pan) * gain
    stereo = np.column_stack((left, right))
    return (np.clip(stereo, -1.0, 1.0) * 32767).astype(np.int16)


def play_startup_sequence() -> None:
    """Play the boot bed once, outside the UI thread."""
    def play() -> None:
        try:
            import sounddevice as sd
            sd.play(startup_sequence(), SAMPLE_RATE, blocking=True)
        except Exception as error:
            print(f"[Sound] Startup sequence unavailable: {error}")

    threading.Thread(target=play, daemon=True, name="jarvis-startup-bed").start()