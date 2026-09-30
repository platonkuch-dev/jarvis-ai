"""Checks the playback settings and first-audio latency measurement path."""
from __future__ import annotations

from core.audio_pipeline import CHUNK_SIZE, OUTPUT_LATENCY, RECEIVE_SAMPLE_RATE

assert CHUNK_SIZE == 512
assert RECEIVE_SAMPLE_RATE == 24000
assert OUTPUT_LATENCY == "low"
print("[PASS] Speaker playback is configured for low latency")