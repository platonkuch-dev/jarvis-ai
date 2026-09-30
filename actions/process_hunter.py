"""
Process Hunter — on-demand suspicious-process scan for the "find suspicious
processes" voice tool, plus a cheap path-only heuristic the HUD's live
THREATS indicator polls every ~1.5s (see ui/sys_metrics.py).

Two different cost budgets, two different functions:
  - count_suspicious_fast(): no subprocess, no signature check — just a
    path/resource heuristic, cheap enough to run on every metrics tick.
  - find_suspicious_processes(): the real scan, run once per voice request.
    Spends real time a background poller couldn't afford: a proper
    two-sample CPU% delta (psutil's single-call cpu_percent() always
    returns 0.0 the first time), a digital-signature check via
    PowerShell's Get-AuthenticodeSignature (subprocess — only for
    processes that already look worth a second look; checking every one
    of ~200 running processes this way would take too long), and
    per-process network connection counts.

"Suspicious" here is a small set of concrete, explainable heuristics, not
a malware classifier — findings are leads for the user to look at
themselves, not verdicts. Reuses windows_control/native.py's
PROTECTED_PROCESS_NAMES so this never flags core OS processes.
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from windows_control.native import PROTECTED_PROCESS_NAMES

_TRUSTED_DIRS = (
    r"c:\windows",
    r"c:\program files",
    r"c:\program files (x86)",
)
# Not inherently malicious on their own — flagged only in combination with
# another reason (high resource use, unsigned, etc.), never alone.
_WATCH_DIRS = (
    "\\appdata\\local\\temp\\",
    "\\downloads\\",
)

_CPU_FLAG_PCT    = 50.0
_MEM_FLAG_MB     = 1500.0

# Legitimate Windows kernel pseudo-processes that have no exe path by design
# (not because anything's hiding) -- exempt them from the "no exe" heuristic
# specifically, on top of windows_control.native's kill-protection list.
_NO_EXE_OK = {"memcompression", "secure system", "registry", "system idle process"}


def _proc_exe_paths() -> list[str]:
    paths = []
    for p in psutil.process_iter(["exe"]):
        try:
            exe = p.info.get("exe")
            if exe:
                paths.append(exe.lower())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return paths


def count_suspicious_fast() -> int:
    """Cheap, no-subprocess heuristic for the HUD's live THREATS number:
    processes running from a watch directory (Temp/Downloads) that ISN'T
    also under a trusted install directory. No signature check, no CPU/RAM
    sampling — just a path check, safe to call every metrics tick."""
    count = 0
    for exe in _proc_exe_paths():
        if any(w in exe for w in _WATCH_DIRS) and not any(exe.startswith(t) for t in _TRUSTED_DIRS):
            count += 1
    return count


@dataclass
class ProcessFinding:
    pid: int
    name: str
    exe: str
    cpu_percent: float
    memory_mb: float
    create_time: str
    connections: int
    signed: bool | None  # None = not checked / undetermined
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


def find_suspicious_processes(parameters: dict | None = None, player=None) -> str:
    parameters = parameters or {}
    top_n = int(parameters.get("limit", 10))

    procs = list(psutil.process_iter(["pid", "name", "exe", "create_time"]))
    # Prime psutil's per-process CPU counters (first call always returns
    # 0.0), then measure the real delta after a short wait.
    for p in procs:
        try:
            p.cpu_percent(interval=None)
        except Exception:
            pass
    time.sleep(0.4)

    findings: list[ProcessFinding] = []
    for p in procs:
        try:
            info = p.info
            name = (info.get("name") or "").strip()
            if not name or name.lower() in PROTECTED_PROCESS_NAMES:
                continue

            cpu    = p.cpu_percent(interval=None)
            mem_mb = p.memory_info().rss / (1024 * 1024)
            exe    = info.get("exe") or ""
            ctime  = info.get("create_time") or 0
            exe_l  = exe.lower()
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
            # second look — checking all ~200 processes would be too slow.
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

            findings.append(ProcessFinding(
                pid=info.get("pid", 0), name=name, exe=exe,
                cpu_percent=round(cpu, 1), memory_mb=round(mem_mb, 1),
                create_time=time.strftime("%H:%M:%S", time.localtime(ctime)) if ctime else "?",
                connections=conns, signed=signed, reasons=reasons,
            ))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    findings.sort(key=lambda f: f.suspicion_score, reverse=True)
    top = findings[:top_n]

    if player is not None and hasattr(player, "show_content"):
        lines = [f"Просканировано процессов: {len(procs)}. Кандидатов: {len(findings)}.\n"]
        for f in top:
            lines.append(f"● {f.name}  (PID {f.pid})")
            lines.append(f"    Путь: {f.exe or '—'}")
            lines.append(f"    CPU: {f.cpu_percent}%   RAM: {f.memory_mb} МБ   Запущен: {f.create_time}")
            sig_txt = "да" if f.signed else ("нет" if f.signed is False else "не проверялась")
            lines.append(f"    Подпись: {sig_txt}")
            lines.append(f"    Причины: {', '.join(f.reasons)}")
            lines.append("")
        player.show_content("Process Hunter", "\n".join(lines))

    if not top:
        return f"Проверил {len(procs)} процессов — подозрительных не нашёл."
    names = ", ".join(f.name for f in top[:3])
    extra = " и другие" if len(findings) > 3 else ""
    return f"Проверил {len(procs)} процессов, нашёл {len(findings)} с признаками для проверки: {names}{extra}. Подробности в панели."
