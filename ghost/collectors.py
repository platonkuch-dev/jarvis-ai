"""
Digital Ghost's 8 state collectors, one per category the user asked for
(PROCESSES, FILES, REGISTRY, SERVICES, NETWORK, DEVICES, WINDOWS EVENTS,
APPLICATIONS). Each collector returns a dict[item_key -> value] snapshot of
"what's currently there" for its category -- ghost/store.py diffs two
consecutive snapshots to find what's new/removed/changed. The one exception
is windows_events(), which is naturally append-only (a log entry never
"changes" or "disappears"), so it returns new entries directly instead of a
keyed state dict -- see its own docstring.

Every collector is real data, no placeholders: psutil for processes/
services/network (already proven in actions/process_hunter.py), winreg for
registry/persistence (no subprocess needed, sub-millisecond), and
PowerShell subprocess calls for the two things Python has no direct API for
(PnP device enumeration, structured event-log queries) -- same pattern
actions/process_hunter.py already uses for Get-AuthenticodeSignature.

Scope notes (engineering judgement, not laziness -- see also the module
docstring in ghost/engine.py):
  - FILES watches a fixed, high-signal directory list (Downloads, Desktop,
    Temp, Startup folders, AppData top level) rather than crawling the
    whole disk every cycle -- a full-disk hash walk every few minutes is
    not what any real endpoint-monitoring tool does either, and would
    turn a lightweight background poll into a disk-thrashing scan.
  - REGISTRY tracks persistence-relevant locations (Run/RunOnce, Winlogon
    Shell/Userinit, Image File Execution Options) plus Scheduled Tasks
    (a different subsystem, same "how does something survive a reboot"
    signal family) rather than the whole registry, which has millions of
    keys most of which never carry any security-relevant information.
  - WINDOWS EVENTS covers System log (service install/start-type-change)
    and Application log (application crashes) only. The Security log
    (account creation, group membership, audit-log-clear) needs the
    process to be elevated or in the Event Log Readers group -- confirmed
    by testing (Get-WinEvent on Security returns an unauthorized-operation
    error under the account JARVIS normally runs as). Not silently
    skipped: engine.py logs this scope explicitly on startup.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import winreg
from pathlib import Path

import psutil

_PS_HIDE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _run_powershell_json(command: str, timeout: float = 20.0):
    """Runs a PowerShell command that ends in `ConvertTo-Json -Compress`
    and returns the parsed result (list, dict, or None on failure/empty)."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, encoding="utf-8", timeout=timeout, **_PS_HIDE,
        )
    except Exception:
        return None
    out = (r.stdout or "").strip()
    if not out:
        return None
    try:
        data = json.loads(out)
    except Exception:
        return None
    return data


# ── 1. PROCESSES ─────────────────────────────────────────────────────────

def processes() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for p in psutil.process_iter(["pid", "name", "exe", "create_time"]):
        try:
            info = p.info
            key = f"{info['pid']}:{info.get('create_time') or 0}"
            out[key] = {"name": info.get("name") or "", "exe": info.get("exe") or ""}
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


# ── 2. FILES ──────────────────────────────────────────────────────────────

_HOME = Path.home()
# (directory, max_depth) -- depth 0 = only the dir's immediate children,
# depth 2 = children and grandchildren. Bounded on purpose, see module docstring.
_WATCHED_DIRS: list[tuple[Path, int]] = [
    (_HOME / "Downloads", 2),
    (_HOME / "Desktop", 2),
    (Path(os.environ.get("TEMP", str(_HOME / "AppData/Local/Temp"))), 2),
    (Path(r"C:\Windows\Temp"), 2),
    (_HOME / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup", 2),
    (Path(r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\StartUp"), 2),
    (_HOME / "AppData/Roaming", 0),   # top-level only -- a new folder appearing here is the signal
    (_HOME / "AppData/Local", 0),     # top-level only, same reason
]


def files() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for base, max_depth in _WATCHED_DIRS:
        if not base.is_dir():
            continue
        base_str = str(base)
        try:
            for root, subdirs, filenames in os.walk(base_str):
                depth = root[len(base_str):].count(os.sep)
                if depth >= max_depth:
                    subdirs[:] = []
                for name in filenames:
                    full = os.path.join(root, name)
                    try:
                        st = os.stat(full)
                    except OSError:
                        continue
                    out[full] = {"size": st.st_size, "mtime": int(st.st_mtime)}
        except OSError:
            continue
    return out


# ── 3. REGISTRY (persistence) ───────────────────────────────────────────

_RUN_KEYS = [
    (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"),
    (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\RunOnce"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\RunOnce"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"),
]
_WINLOGON_KEY = (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows NT\CurrentVersion\Winlogon")
_WINLOGON_VALUES = ("Shell", "Userinit")
_IFEO_KEY = (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows NT\CurrentVersion\Image File Execution Options")


def _enum_values(hive, path) -> list[tuple[str, str]]:
    out = []
    try:
        with winreg.OpenKey(hive, path) as k:
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(k, i)
                except OSError:
                    break
                out.append((name, str(value)))
                i += 1
    except FileNotFoundError:
        pass
    except OSError:
        pass
    return out


def _ifeo_debuggers() -> list[tuple[str, str]]:
    """IFEO 'Debugger' values are a classic hijack vector (redirect a
    legitimate exe's launch to something else) -- most subkeys here are
    empty/legitimate (debug tooling), so only subkeys that actually SET a
    Debugger value are worth tracking."""
    out = []
    hive, path = _IFEO_KEY
    try:
        with winreg.OpenKey(hive, path) as k:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(k, sub) as sk:
                        try:
                            debugger = winreg.QueryValueEx(sk, "Debugger")[0]
                            out.append((sub, str(debugger)))
                        except FileNotFoundError:
                            pass
                except OSError:
                    continue
    except FileNotFoundError:
        pass
    except OSError:
        pass
    return out


def _scheduled_tasks() -> list[tuple[str, str]]:
    """Not registry, but the same 'how does this survive a reboot'
    persistence signal family -- kept in this collector rather than
    inventing a 9th category the user didn't ask for."""
    data = _run_powershell_json(
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "Get-ScheduledTask | Where-Object {$_.State -ne 'Disabled'} | "
        "Select-Object TaskName, TaskPath, State | ConvertTo-Json -Compress",
        timeout=15.0,
    )
    if not data:
        return []
    if isinstance(data, dict):
        data = [data]
    out = []
    for t in data:
        key = f"{t.get('TaskPath', '')}{t.get('TaskName', '')}"
        out.append((key, str(t.get("State", ""))))
    return out


def registry() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for hive, path in _RUN_KEYS:
        for name, value in _enum_values(hive, path):
            out[f"run:{path}\\{name}"] = {"value": value}

    winlogon_hive, winlogon_path = _WINLOGON_KEY
    for value_name in _WINLOGON_VALUES:
        try:
            with winreg.OpenKey(winlogon_hive, winlogon_path) as k:
                value = winreg.QueryValueEx(k, value_name)[0]
                out[f"winlogon:{value_name}"] = {"value": str(value)}
        except (FileNotFoundError, OSError):
            pass

    for sub, debugger in _ifeo_debuggers():
        out[f"ifeo:{sub}"] = {"value": debugger}

    for key, state in _scheduled_tasks():
        out[f"task:{key}"] = {"value": state}

    return out


# ── 4. SERVICES ──────────────────────────────────────────────────────────

def services() -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        for s in psutil.win_service_iter():
            try:
                d = s.as_dict()
            except Exception:
                continue
            out[d["name"]] = {
                "status": d.get("status", ""),
                "start_type": d.get("start_type", ""),
                "binpath": d.get("binpath", ""),
            }
    except Exception:
        pass
    return out


# ── 5. NETWORK (connections + DNS resolutions) ──────────────────────────

def network() -> dict[str, dict]:
    """Tracks distinct remote destination IPs, not every ephemeral
    connection tuple -- raw connections churn too fast (thousands of
    open/close events per hour during normal browsing) to be a useful
    'what's new' signal; a new destination IP JARVIS hasn't seen this
    process talk to before is the actually-interesting signal, matching
    the spec's own 'network destinations' framing."""
    out: dict[str, dict] = {}
    try:
        for c in psutil.net_connections(kind="inet"):
            if not c.raddr:
                continue
            ip = c.raddr.ip
            if ip in out:
                continue
            out[ip] = {"port": c.raddr.port, "pid": c.pid or 0}
    except (psutil.AccessDenied, OSError):
        pass
    return out


def dns_cache() -> dict[str, dict]:
    """Hostnames currently in the local DNS resolver cache -- the 'DNS
    REQUEST' signal from the spec's example correlation chain. Parses only
    the hostname header lines (immediately followed by a '----' divider),
    deliberately ignoring ipconfig's localized field labels below each
    entry so this works regardless of the system's display language."""
    out: dict[str, dict] = {}
    try:
        r = subprocess.run(["ipconfig", "/displaydns"], capture_output=True, timeout=10, **_PS_HIDE)
    except Exception:
        return out
    text = (r.stdout or b"").decode("ascii", errors="ignore")
    lines = text.splitlines()
    for i in range(len(lines) - 1):
        candidate = lines[i].strip()
        if candidate and re.fullmatch(r"-{5,}", lines[i + 1].strip()) and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9.\-]*", candidate
        ):
            out[candidate.lower()] = {}
    return out


# ── 6. DEVICES (USB / PnP) ──────────────────────────────────────────────

def devices() -> dict[str, dict]:
    data = _run_powershell_json(
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "Get-PnpDevice -PresentOnly | Select-Object InstanceId, FriendlyName, Class, Status | "
        "ConvertTo-Json -Compress",
        timeout=20.0,
    )
    if not data:
        return {}
    if isinstance(data, dict):
        data = [data]
    out: dict[str, dict] = {}
    for d in data:
        inst = d.get("InstanceId")
        if not inst:
            continue
        out[inst] = {
            "name": d.get("FriendlyName") or "",
            "class": d.get("Class") or "",
            "status": d.get("Status") or "",
        }
    return out


# ── 7. WINDOWS EVENTS (append-only -- see engine.py for how this is used) ──

# (LogName, [EventIds]) -- see module docstring for why Security log is excluded.
_EVENT_SOURCES = [
    ("System", [7045, 7040]),        # service installed / start-type changed
    ("Application", [1000]),         # application crash (Application Error)
]


def windows_events(since_ts: float) -> list[dict]:
    """Returns NEW log entries with TimeCreated > since_ts, oldest first.
    Unlike every other collector this is not a keyed state snapshot --
    event log entries don't get 'removed' or 'changed', they just
    accumulate, so there's nothing to diff against a previous snapshot."""
    results: list[dict] = []
    for log_name, ids in _EVENT_SOURCES:
        ids_ps = ",".join(str(i) for i in ids)
        cmd = (
            "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
            f"$since = [DateTimeOffset]::FromUnixTimeSeconds({int(since_ts)}).LocalDateTime; "
            f"try {{ Get-WinEvent -FilterHashtable @{{LogName='{log_name}'; Id={ids_ps}; StartTime=$since}} "
            "-ErrorAction Stop | Select-Object Id, TimeCreated, "
            "@{N='Msg';E={$_.Message.Split(\"`n\")[0].TrimEnd(\"`r\")}} | ConvertTo-Json -Compress } "
            "catch { Write-Output '[]' }"
        )
        data = _run_powershell_json(cmd, timeout=15.0)
        if not data:
            continue
        if isinstance(data, dict):
            data = [data]
        for e in data:
            ts_raw = e.get("TimeCreated", "")
            m = re.search(r"/Date\((\d+)\)/", str(ts_raw))
            ts = int(m.group(1)) / 1000.0 if m else since_ts
            results.append({
                "log": log_name, "id": e.get("Id"), "ts": ts,
                "message": (e.get("Msg") or "")[:200],
            })
    results.sort(key=lambda e: e["ts"])
    return results


# ── 8. APPLICATIONS (installed programs) ────────────────────────────────

_UNINSTALL_KEYS = [
    (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
]


def applications() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for hive, path in _UNINSTALL_KEYS:
        try:
            with winreg.OpenKey(hive, path) as k:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(k, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        with winreg.OpenKey(k, sub) as sk:
                            try:
                                name = winreg.QueryValueEx(sk, "DisplayName")[0]
                            except FileNotFoundError:
                                continue
                            try:
                                ver = winreg.QueryValueEx(sk, "DisplayVersion")[0]
                            except FileNotFoundError:
                                ver = ""
                            out[str(name)] = {"version": str(ver)}
                    except OSError:
                        continue
        except FileNotFoundError:
            continue
        except OSError:
            continue
    return out


# State-diffed categories (windows_events is handled separately by engine.py).
STATE_COLLECTORS = {
    "processes": processes,
    "files": files,
    "registry": registry,
    "services": services,
    "network": network,
    "dns": dns_cache,
    "devices": devices,
    "applications": applications,
}
