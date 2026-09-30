"""Local PC health report for the JARVIS diagnostics command."""
from __future__ import annotations

import platform
import shutil
from pathlib import Path

import psutil

from actions.system_monitor import get_system_status


def system_diagnostics(parameters: dict | None = None, player=None) -> str:
    status = get_system_status()
    home = Path.home()
    disk = shutil.disk_usage(home)
    startup = len([entry for entry in psutil.process_iter(["name"])])
    lines = [
        "SYSTEM DIAGNOSTICS",
        f"OS: {platform.platform()}",
        f"CPU: {status['cpu_percent']}% | RAM: {status['ram_percent']}% ({status['ram_used_gb']}/{status['ram_total_gb']} GB)",
        f"GPU: {status['gpu_percent'] if status['gpu_percent'] is not None else 'unavailable'}% | Temperature: {status['cpu_temp_c'] if status['cpu_temp_c'] is not None else 'unavailable'} C",
        f"Disk ({home.drive or home.anchor}): {disk.free / 1024 ** 3:.1f} GB free of {disk.total / 1024 ** 3:.1f} GB",
        f"Processes: {startup} | Uptime: {status['uptime']}",
        "Security: use 'scan the system' to run Microsoft Defender.",
    ]
    report = "\n".join(lines)
    if player:
        player.show_content("PC diagnostics", report)
    return report