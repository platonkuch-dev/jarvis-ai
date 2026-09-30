"""Checks the local STT settings used for short Russian voice commands."""
from __future__ import annotations

import numpy as np

from voice.stt import SpeechToText


class FakeModel:
    def __init__(self) -> None:
        self.kwargs: dict = {}

    def transcribe(self, _audio, **kwargs):
        self.kwargs = kwargs
        return [], None


stt = SpeechToText()
model = FakeModel()
stt._model = model
assert stt.transcribe(np.zeros(1600, dtype=np.float32)) == ""
assert model.kwargs["language"] == "ru"
assert model.kwargs["beam_size"] == 3
assert model.kwargs["condition_on_previous_text"] is False
assert model.kwargs["vad_filter"] is False
print("[PASS] STT uses stable Russian short-command settings")