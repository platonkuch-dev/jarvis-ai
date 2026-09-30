"""hud_head3d: orb -> assembly -> states -> sleep -> wake, painted without a screen."""

from __future__ import annotations

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

hud_head3d = pytest.importorskip("hud_head3d")


@pytest.fixture(scope="module")
def app():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _run(panel, clock, seconds):
    for _ in range(int(seconds / 0.016)):
        clock[0] += 0.016
        panel._step()
        panel.grab()


def test_full_cycle(app, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(hud_head3d.time, "monotonic", lambda: clock[0])
    head = hud_head3d.HeadPanel()
    head._rx.ok = False
    assert head.isVisible() and head._mode == "orb"          # on screen from the start

    head.set_status("listening")
    head.play_assembly()
    _run(head, clock, 1.5)
    assert head._mode == "assemble" and 0 < head._progress < 1
    _run(head, clock, 2.5)
    assert head._mode == "shown"

    def loud(now):
        head._level, head._sib, head._last_audio = 0.9, 0.1, now

    real = head._pull_audio
    head._pull_audio = loud
    head.set_status("speaking")
    _run(head, clock, 0.4)
    assert head._open > 0.4
    head._pull_audio = real

    head.set_status("thinking")
    _run(head, clock, 1.0)
    assert head._think > 0.5

    head.set_status("sleeping")
    _run(head, clock, 2.0)
    assert head._mode == "orb" and head.isVisible()           # standby orb stays

    head.set_status("listening")
    _run(head, clock, 0.1)
    assert head._mode == "assemble"


def test_jaw_opens_the_mouth(app):
    head = hud_head3d.HeadPanel()
    head._rx.ok = False
    m = head._m
    _, closed_y, _, _ = head._pose()
    head._open = 0.8
    _, open_y, _, _ = head._pose()
    jaw = m.jaw_w > 0.8
    assert jaw.any() and np.mean(open_y[jaw] - closed_y[jaw]) > 5      # pixels
    head.hide()


def test_bottom_right_corner(app):
    from PyQt6.QtWidgets import QApplication

    head = hud_head3d.HeadPanel()
    g = QApplication.primaryScreen().availableGeometry()
    assert head.x() + head.width() <= g.x() + g.width()
    assert head.x() > g.x() + g.width() / 2 and head.y() > g.y() + g.height() / 2
    head.hide()
