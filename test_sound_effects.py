"""Unit checks for generated JARVIS sound effects; does not play audio."""
import numpy as np

from core.sound_effects import SAMPLE_RATE, startup_sequence

startup = startup_sequence()
assert startup.dtype == np.int16
assert startup.shape == (int(SAMPLE_RATE * 5.4), 2)
assert np.max(np.abs(startup)) > 100
print("[PASS] Startup sequence is valid PCM")
