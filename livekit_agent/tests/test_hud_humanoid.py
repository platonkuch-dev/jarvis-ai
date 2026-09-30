"""hud_humanoid: the whole show cycle runs and paints without a screen."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

hud_humanoid = pytest.importorskip("hud_humanoid")


@pytest.fixture(scope="module")
def app():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _run(panel, clock, seconds):
    for _ in range(int(seconds / 0.016)):
        clock[0] += 0.016
        panel._step()
        panel.grab()          # forces a real paintEvent


def test_stream_in_talk_and_stream_back(app, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(hud_humanoid.time, "monotonic", lambda: clock[0])
    panel = hud_humanoid.HumanoidPanel(lambda: (500, 60))
    panel._rx.ok = False

    panel.play_assembly()
    _run(panel, clock, 2.0)
    assert panel._mode == "assemble" and 0.0 < panel._progress < 1.0
    _run(panel, clock, 2.7)
    assert panel._mode == "shown"

    def loud(now):
        panel._level, panel._sib, panel._last_audio = 0.9, 0.1, now

    real_pull = panel._pull_audio
    panel._pull_audio = loud
    panel.set_speaking(True)
    _run(panel, clock, 0.4)
    assert panel._energy > 0.5
    panel._pull_audio = real_pull

    panel.set_speaking(False)
    panel._level, panel._last_audio = 0.0, 0.0
    _run(panel, clock, 9.5)
    assert panel._mode == "hidden"

    panel.set_speaking(True)
    _run(panel, clock, 0.1)
    assert panel._mode == "materialize"
    _run(panel, clock, 2.0)
    assert panel._mode == "shown"


def test_figure_is_a_bust_with_a_face_core():
    fig = hud_humanoid._Figure()
    kinds = set(fig.kind.tolist())
    assert {hud_humanoid.KIND_FACE, hud_humanoid.KIND_RIM, hud_humanoid.KIND_CONTOUR,
            hud_humanoid.KIND_NECK, hud_humanoid.KIND_DUST} <= kinds
    face = fig.kind == hud_humanoid.KIND_FACE
    assert abs(fig.x[face].mean() - hud_humanoid.CX) < 3        # core centred on the head
    assert fig.halfw(300) > 2.5 * fig.halfw(215)                 # shoulders much wider than the neck
