"""Non-destructive checks for Microsoft Defender scan orchestration."""
from __future__ import annotations

import threading
from unittest.mock import patch

from actions.system_scan import DefenderScanManager, ScanWallpaper


def check(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"[PASS] {label}")


class FakeProcess:
    def __init__(self):
        self.returncode = 0

    def poll(self):
        return 0

    def communicate(self):
        return "", ""


class FakeWallpaper:
    def __init__(self):
        self.events: list[str] = []

    def activate(self) -> None:
        self.events.append("activate")

    def restore(self) -> None:
        self.events.append("restore")


class FakeWallpaperEngineProcess:
    def __init__(self, name: str, command: list[str]):
        self.info = {"name": name, "cmdline": command, "exe": command[0]}
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True


def test_quick_scan_lifecycle() -> None:
    manager = DefenderScanManager()
    wallpaper = FakeWallpaper()
    manager._wallpaper = wallpaper
    stages: list[str] = []
    finished = threading.Event()

    def status(stage: str, _message: str) -> None:
        stages.append(stage)
        if stage in {"COMPLETE", "THREATS", "ERROR"}:
            finished.set()

    with patch("actions.system_scan.platform.system", return_value="Windows"), \
         patch("actions.system_scan.subprocess.Popen", return_value=FakeProcess()), \
         patch.object(manager, "_threats_since", return_value=0), \
         patch("actions.system_scan.time.sleep", return_value=None):
        result = manager.start("quick", status)
        check("quick scan starts", "quick scan started" in result.lower())
        check("quick scan reaches completion", finished.wait(1))
    check("Defender lifecycle reports preparation, scan, and completion", stages == ["PREPARING", "SCANNING", "COMPLETE"])
    check("temporary scan wallpaper is applied then restored", wallpaper.events == ["activate", "restore"])


def test_concurrent_start_is_rejected() -> None:
    manager = DefenderScanManager()
    with patch("actions.system_scan.platform.system", return_value="Windows"):
        with manager._lock:
            manager._running = True
        check("second scan is not started", "already running" in manager.start("full", lambda *_: None).lower())


def test_wallpaper_engine_lifecycle() -> None:
    wallpaper = ScanWallpaper()
    engine = FakeWallpaperEngineProcess("wallpaper64.exe", [r"C:\Wallpaper Engine\wallpaper64.exe", "-silent"])
    applied: list[str] = []
    launched: list[list[str]] = []
    with patch("actions.system_scan.platform.system", return_value="Windows"), \
         patch("actions.system_scan.psutil.process_iter", return_value=[engine]), \
         patch("actions.system_scan.psutil.wait_procs", return_value=([engine], [])), \
         patch.object(ScanWallpaper, "_current_wallpaper", return_value=r"C:\Wallpapers\original.jpg"), \
         patch.object(ScanWallpaper, "_write_bitmap"), \
         patch.object(ScanWallpaper, "_apply", side_effect=lambda path: applied.append(path) or True), \
         patch("actions.system_scan.subprocess.Popen", side_effect=lambda command, **_kwargs: launched.append(command)):
        wallpaper.activate()
        wallpaper.restore()
    check("Wallpaper Engine is stopped for the scan wallpaper", engine.terminated)
    check("scan wallpaper is restored to the original Windows wallpaper", applied[-1] == r"C:\Wallpapers\original.jpg")
    check("Wallpaper Engine is restarted with its original command", launched == [[r"C:\Wallpaper Engine\wallpaper64.exe", "-silent"]])


test_quick_scan_lifecycle()
test_concurrent_start_is_rejected()
test_wallpaper_engine_lifecycle()