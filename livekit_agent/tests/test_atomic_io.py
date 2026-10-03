from __future__ import annotations

import os

import pytest

import atomic_io


def test_retries_while_reader_holds_the_file(tmp_path, monkeypatch):
    target = tmp_path / "hud_state.json"
    real_replace, calls = os.replace, []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) < 3:  # another process has it open for two attempts
            raise PermissionError(5, "Отказано в доступе")
        real_replace(src, dst)

    monkeypatch.setattr(atomic_io.os, "replace", flaky)
    monkeypatch.setattr(atomic_io, "_DELAY_S", 0)
    atomic_io.atomic_write_text(target, '{"status": "listening"}')
    assert target.read_text("utf-8") == '{"status": "listening"}' and len(calls) == 3
    assert not list(tmp_path.glob("*.tmp"))


def test_gives_up_and_cleans_tmp(tmp_path, monkeypatch):
    def always_locked(src, dst):
        raise PermissionError(5, "Отказано в доступе")

    monkeypatch.setattr(atomic_io.os, "replace", always_locked)
    monkeypatch.setattr(atomic_io, "_DELAY_S", 0)
    with pytest.raises(PermissionError):
        atomic_io.atomic_write_text(tmp_path / "mic_state.json", "{}")
    assert not list(tmp_path.glob("*.tmp"))
