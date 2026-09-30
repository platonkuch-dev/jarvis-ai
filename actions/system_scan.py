"""Microsoft Defender scan integration for JARVIS on Windows."""
from __future__ import annotations

import platform
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from core.path_utils import get_user_data_dir

try:
    import psutil
except ImportError:
    psutil = None


StatusCallback = Callable[[str, str], None]


class ScanWallpaper:
    """Temporarily applies a locally generated security-scan wallpaper."""
    _WALLPAPER_ENGINE_PROCESSES = {"wallpaper32.exe", "wallpaper64.exe", "wallpaper_engine.exe"}

    def __init__(self) -> None:
        self._original: str | None = None
        self._wallpaper_engine_commands: list[list[str]] = []

    def _pause_wallpaper_engine(self) -> None:
        """Stop Wallpaper Engine's desktop renderer so Windows can show the scan wallpaper."""
        if psutil is None:
            return
        processes = []
        for process in psutil.process_iter(["name", "cmdline", "exe"]):
            try:
                if (process.info.get("name") or "").lower() not in self._WALLPAPER_ENGINE_PROCESSES:
                    continue
                command = process.info.get("cmdline") or []
                executable = process.info.get("exe") or ""
                if not command and executable:
                    command = [executable]
                if command and command[0]:
                    self._wallpaper_engine_commands.append(list(command))
                process.terminate()
                processes.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if processes:
            _, still_running = psutil.wait_procs(processes, timeout=3)
            for process in still_running:
                try:
                    process.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            time.sleep(0.5)

    def _resume_wallpaper_engine(self) -> None:
        commands, self._wallpaper_engine_commands = self._wallpaper_engine_commands, []
        for command in commands:
            try:
                subprocess.Popen(command, cwd=str(Path(command[0]).parent))
            except Exception as error:
                print(f"[SecurityScan] Wallpaper Engine restart failed: {error}")

    @staticmethod
    def _write_bitmap(path: Path) -> None:
        width, height = 1280, 720
        row_size = (width * 3 + 3) & ~3
        pixels = bytearray(row_size * height)
        for y in range(height):
            for x in range(width):
                grid = x % 64 == 0 or y % 64 == 0
                diagonal = (x + y * 2) % 180 < 2
                pulse = int(10 + 12 * ((x + y) % 180) / 180)
                green = 50 if grid else 22 + pulse
                blue = 18 if diagonal else 10
                offset = (height - 1 - y) * row_size + x * 3
                pixels[offset:offset + 3] = bytes((blue, green, 4))
        header = bytearray(54)
        header[0:2] = b"BM"
        header[2:6] = (54 + len(pixels)).to_bytes(4, "little")
        header[10:14] = (54).to_bytes(4, "little")
        header[14:18] = (40).to_bytes(4, "little")
        header[18:22] = width.to_bytes(4, "little", signed=True)
        header[22:26] = height.to_bytes(4, "little", signed=True)
        header[26:28] = (1).to_bytes(2, "little")
        header[28:30] = (24).to_bytes(2, "little")
        path.write_bytes(header + pixels)

    @staticmethod
    def _current_wallpaper() -> str | None:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop") as key:
                return str(winreg.QueryValueEx(key, "WallPaper")[0]) or None
        except Exception:
            return None

    @staticmethod
    def _apply(path: str) -> bool:
        try:
            import ctypes
            return bool(ctypes.windll.user32.SystemParametersInfoW(20, 0, path, 3))
        except Exception:
            return False

    def activate(self) -> None:
        if platform.system() != "Windows" or self._original is not None:
            return
        self._original = self._current_wallpaper()
        try:
            self._pause_wallpaper_engine()
            wallpaper = get_user_data_dir() / "defender_scan_wallpaper.bmp"
            self._write_bitmap(wallpaper)
            self._apply(str(wallpaper))
        except Exception as error:
            print(f"[SecurityScan] Wallpaper activation failed: {error}")

    def restore(self) -> None:
        original, self._original = self._original, None
        if original:
            self._apply(original)
        self._resume_wallpaper_engine()


class DefenderScanManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._wallpaper = ScanWallpaper()

    def start(self, scan_type: str, on_status: StatusCallback) -> str:
        if platform.system() != "Windows":
            return "System scanning is currently available only on Windows through Microsoft Defender."
        scan_type = "FullScan" if scan_type.strip().lower() in {"full", "fullscan", "complete"} else "QuickScan"
        with self._lock:
            if self._running:
                return "A Microsoft Defender scan is already running."
            self._running = True
        self._wallpaper.activate()
        threading.Thread(target=self._run, args=(scan_type, on_status), daemon=True, name="defender-scan").start()
        label = "full" if scan_type == "FullScan" else "quick"
        return f"Microsoft Defender {label} scan started. I will report the result when it finishes."

    def _run(self, scan_type: str, on_status: StatusCallback) -> None:
        started = datetime.now()
        try:
            on_status("PREPARING", "Preparing Microsoft Defender and checking protection status...")
            time.sleep(0.4)
            on_status("SCANNING", f"Microsoft Defender is running a {scan_type.replace('Scan', ' scan').lower()}.")
            command = [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                f"Start-MpScan -ScanType {scan_type}",
            ]
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            last_update = time.monotonic()
            while process.poll() is None:
                if time.monotonic() - last_update >= 12:
                    on_status("SCANNING", "Defender is still scanning system files and active locations...")
                    last_update = time.monotonic()
                time.sleep(0.5)
            _, error = process.communicate()
            if process.returncode:
                on_status("ERROR", f"Microsoft Defender could not complete the scan: {error.strip()[:240] or 'unknown error'}")
                return

            threats = self._threats_since(started)
            if threats:
                on_status("THREATS", f"Scan complete. Microsoft Defender reported {threats} recent threat detection(s). Open Windows Security for details.")
            else:
                on_status("COMPLETE", "Scan complete. Microsoft Defender reported no new threats.")
        except Exception as error:
            on_status("ERROR", f"System scan failed: {error}")
        finally:
            self._wallpaper.restore()
            with self._lock:
                self._running = False

    @staticmethod
    def _threats_since(started: datetime) -> int:
        command = (
            "$since=(Get-Date).AddMinutes(-2); "
            "@(Get-MpThreatDetection -ErrorAction SilentlyContinue | "
            "Where-Object {$_.InitialDetectionTime -ge $since}).Count"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True, text=True, timeout=12,
            )
            return int(result.stdout.strip() or "0")
        except Exception:
            return 0


_manager = DefenderScanManager()


def system_scan(parameters: dict | None = None, player=None, on_status: StatusCallback | None = None) -> str:
    scan_type = (parameters or {}).get("scan_type", "quick")

    def report(stage: str, message: str) -> None:
        if player:
            player.write_log(f"[SECURITY] {message}")
            player.show_content("Security scan", f"{stage}\n\n{message}")
            set_scan = getattr(player, "set_security_scan", None)
            if set_scan:
                set_scan(stage not in {"COMPLETE", "THREATS", "ERROR"}, message)
        if on_status:
            on_status(stage, message)

    return _manager.start(str(scan_type), report)