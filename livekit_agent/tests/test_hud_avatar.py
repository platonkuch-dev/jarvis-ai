"""hud_avatar: the whole show cycle runs and paints without a screen."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

hud_avatar = pytest.importorskip("hud_avatar")
pytestmark = pytest.mark.skipif(not hud_avatar.assets_ready(), reason="run scripts/make_avatar.py first")


@pytest.fixture(scope="module")
def app():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _run(panel, clock, seconds):
    for _ in range(int(seconds / 0.016)):
        clock[0] += 0.016
        panel._step()
        panel.grab()          # forces a real paintEvent


def test_launch_assembly_then_talk_then_dissolve(app, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(hud_avatar.time, "monotonic", lambda: clock[0])
    panel = hud_avatar.AvatarPanel(lambda: (500, 60))
    panel._rx.ok = False       # no real UDP in tests

    panel.play_assembly()
    assert panel._mode == "assemble"
    _run(panel, clock, 3.6)
    assert panel._mode == "shown" and panel._reveal == 1.0

    panel.set_speaking(True)
    real_pull = panel._pull_audio

    def loud(now):             # stands in for a stream of lipsync packets
        panel._level, panel._sib, panel._last_audio = 0.8, 0.1, now

    panel._pull_audio = loud
    _run(panel, clock, 0.5)
    assert panel._open > 0.3
    panel._pull_audio = real_pull

    panel.set_speaking(False)
    panel._level, panel._last_audio = 0.0, 0.0
    _run(panel, clock, 8.5)     # greeting linger + hold run out -> dissolve -> hidden
    assert panel._mode == "hidden"

    panel.set_speaking(True)
    _run(panel, clock, 0.1)
    assert panel._mode == "materialize"
    _run(panel, clock, 1.4)
    assert panel._mode == "shown"
