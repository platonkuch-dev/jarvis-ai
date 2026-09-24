"""Process Hunter — on-demand suspicious-process scan.

"Suspicious" here is a small set of concrete, explainable heuristics, not a
malware classifier: findings are leads for the user to look at themselves,
never a verdict, and nothing is ever killed automatically -- use
window_manager's "close" action separately if the user decides to act on a
finding. Read-only, so no confirmation gate is needed.

Spends real time a background poller couldn't afford: a proper two-sample
CPU% delta (psutil's single-call cpu_percent() always returns 0.0 the first
time), a digital-signature check via PowerShell's Get-AuthenticodeSignature
(only for processes that already look worth a second look -- checking every
one of ~200 running processes this way would take too long), and per-process
network connection counts. Reuses windows_control.native's
PROTECTED_PROCESS_NAMES so this never flags core OS processes.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil
from livekit.agents import RunContext, function_tool

import config
from tools._logging import log_call
from tools.registry import register_impl, register_tool

_TRUSTED_DIRS = (
    r"c:\windows",
    r"c:\program files",
    r"c:\program files (x86)",
)
# Not inherently malicious on their own -- flagged only in combination with
# another reason (high resource use, unsigned, etc.), never alone.
_WATCH_DIRS = (
    "\\appdata\\local\\temp\\",
    "\\downloads\\",
)

_CPU_FLAG_PCT = 50.0
_MEM_FLAG_MB = 1500.0

# Legitimate Windows kernel pseudo-processes that have no exe path by design
# (not because anything's hiding) -- exempt from the "no exe" heuristic on
# top of windows_control.native's kill-protection list.
_NO_EXE_OK = {"memcompression", "secure system", "registry", "system idle process"}


@dataclass
class _ProcessFinding:
    pid: int
    name: str
    exe: str
    cpu_percent: float
    memory_mb: float
    create_time: str
    connections: int
    signed: bool | None
    reasons: list[str] = field(default_factory=list)

    @property
    def suspicion_score(self) -> int:
        return len(self.reasons)


def _check_signature(exe_path: str, timeout: float = 3.0) -> bool | None:
    if not exe_path:
        return None
    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                f"(Get-AuthenticodeSignature -LiteralPath '{exe_path}').Status",
            ],
            capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return result.stdout.strip() == "Valid"
    except Exception:
        return None


def _connection_count(proc: psutil.Process) -> int:
    try:
        return len(proc.net_connections(kind="inet"))
    except Exception:
        return 0


def _scan(limit: int) -> list[_ProcessFinding]:
    from windows_control.native import PROTECTED_PROCESS_NAMES

    procs = list(psutil.process_iter(["pid", "name", "exe", "create_time"]))
    # Prime psutil's per-process CPU counters (first call always returns
    # 0.0), then measure the real delta after a short wait.
    for p in procs:
        try:
            p.cpu_percent(interval=None)
        except Exception:
            pass
    time.sleep(0.4)

    findings: list[_ProcessFinding] = []
    for p in procs:
        try:
            info = p.info
            name = (info.get("name") or "").strip()
            if not name or name.lower() in PROTECTED_PROCESS_NAMES:
                continue

            cpu = p.cpu_percent(interval=None)
            mem_mb = p.memory_info().rss / (1024 * 1024)
            exe = info.get("exe") or ""
            ctime = info.get("create_time") or 0
            exe_l = exe.lower()
            in_trusted_dir = any(exe_l.startswith(d) for d in _TRUSTED_DIRS) or name.lower() in _NO_EXE_OK

            reasons: list[str] = []
            if exe and any(d in exe_l for d in _WATCH_DIRS) and not in_trusted_dir:
                reasons.append(f"запущен из {Path(exe).parent}")
            if cpu >= _CPU_FLAG_PCT:
                reasons.append(f"высокая загрузка CPU ({cpu:.0f}%)")
            if mem_mb >= _MEM_FLAG_MB:
                reasons.append(f"много памяти ({mem_mb / 1024:.1f} ГБ)")
            if not exe and name.lower() not in _NO_EXE_OK:
                reasons.append("нет пути к исполняемому файлу")

            # Signature check only for candidates that already look worth a
            # second look -- checking all ~200 processes would be too slow.
            signed: bool | None = None
            if exe and (reasons or not in_trusted_dir):
                signed = _check_signature(exe)
                if signed is False:
                    reasons.append("не подписан цифровой подписью")

            if not reasons:
                continue

            conns = _connection_count(p)
            if conns > 0:
                reasons.append(f"{conns} сетевых соединений")

            findings.append(_ProcessFinding(
                pid=info.get("pid", 0), name=name, exe=exe,
                cpu_percent=round(cpu, 1), memory_mb=round(mem_mb, 1),
                create_time=time.strftime("%H:%M:%S", time.localtime(ctime)) if ctime else "?",
                connections=conns, signed=signed, reasons=reasons,
            ))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    findings.sort(key=lambda f: f.suspicion_score, reverse=True)
    return findings[:limit]


@register_impl("find_suspicious_processes")
@log_call("find_suspicious_processes")
async def _find_suspicious_processes(*, limit: int = 10) -> dict:
    if config.SYSTEM != "Windows":
        return {"status": "error", "message": "Проверка процессов реализована только для Windows."}

    findings = await asyncio.to_thread(_scan, limit)

    if not findings:
        return {"status": "ok", "message": "Проверил запущенные процессы — подозрительных не нашёл."}

    lines = [f"Нашёл {len(findings)} процесс(ов) с признаками для проверки:"]
    for f in findings:
        sig_txt = "да" if f.signed else ("нет" if f.signed is False else "не проверялась")
        lines.append(
            f"- {f.name} (PID {f.pid}), CPU {f.cpu_percent}%, RAM {f.memory_mb} МБ, "
            f"подпись: {sig_txt}. Причины: {', '.join(f.reasons)}."
        )
    return {"status": "ok", "message": "\n".join(lines), "findings": [f.__dict__ for f in findings]}


@register_tool
@function_tool
async def find_suspicious_processes(context: RunContext, limit: int = 10) -> str:
    """Scan running processes for concrete red flags (running from Temp/
    Downloads, unusually high CPU/RAM, unsigned executable, no exe path) and
    report the top candidates. This is a set of heuristic leads for the user
    to look at, not a malware verdict, and never kills anything itself.

    Args:
        limit: Maximum number of flagged processes to report (default 10).
    """
    result = await _find_suspicious_processes(limit=limit)
    return result["message"]
