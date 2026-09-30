"""Checks fast routing for local status and JARVIS management commands."""
from __future__ import annotations

from unittest.mock import patch

from voice.fast_router import route


with patch("actions.jarvis_control.jarvis_control", return_value="Focus mode enabled."):
    result = route("включи режим фокуса")
    assert result.matched and result.rule_name == "focus_on"

with patch("actions.jarvis_control.jarvis_control", return_value="Power Mode disabled."):
    result = route("выключи пауэр мод")
    assert result.matched and result.rule_name == "power_off"

with patch("actions.system_monitor.get_system_status", return_value={"cpu_percent": 12, "ram_percent": 34, "gpu_percent": 56}):
    result = route("статус компьютера")
    assert result.matched and result.rule_name == "system_status" and "CPU 12%" in result.message

print("[PASS] Expanded local Fast Path routes system and JARVIS controls")