"""Multi-strategy application launcher, ported from the original Jarvis
project's actions/open_app.py rather than re-invented -- the simple
os.startfile(name) this project's open_application originally used only
works for the handful of apps that register a PATH entry or a plain "App
Paths" registry key. Most consumer apps (Discord, Spotify, CapCut, OBS...)
do neither: confirmed live on this machine that "Discord" has a real Start
Menu shortcut and IS installed, yet a bare os.startfile("Discord") fails --
exactly the false "not installed" the user hit.

Escalating strategy, fastest/most reliable first:
    1. A cached resolution from a previous successful launch of this app.
    2. PATH (covers notepad, calc, explorer, cmd, code, ...).
    3. Windows "App Paths" registry (HKCU/HKLM) -- where most installers
       register their main executable even without adding it to PATH.
    4. An indexed Start Menu .lnk shortcut (covers almost everything else:
       Discord, Spotify, WhatsApp, Telegram, most Electron apps).
    5. Last resort: type the name into the Start Menu search and press
       Enter -- the widest possible net, since it's the same search a human
       would use.

Every step verifies a real new process actually appeared (via psutil) before
declaring success, and remembers already-open apps so a second "open X"
just focuses them instead of waiting for a process that will never spawn.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import psutil

import config

_SYSTEM = config.SYSTEM


def _normalize(raw: str) -> str:
    key = raw.lower().strip()
    entry = config.APP_ALIASES.get(key)
    if entry is not None:
        return entry.get(_SYSTEM, raw)
    for alias_key, os_map in config.APP_ALIASES.items():
        if alias_key in key or key in alias_key:
            return os_map.get(_SYSTEM, raw)
    return raw


def _snapshot_process_names() -> set[str]:
    try:
        return {(p.info.get("name") or "").lower() for p in psutil.process_iter(["name"])}
    except Exception:
        return set()


def _process_name_matches(names: set[str], stem: str) -> bool:
    for proc_name in names:
        proc_stem = proc_name.replace(".exe", "").replace(" ", "")
        if proc_stem and (stem in proc_stem or proc_stem in stem):
            return True
    return False


def _wait_for_new_process(before: set[str], app_name: str, timeout: float = 3.0) -> bool:
    """Poll for a process whose name plausibly matches app_name. Checks
    `before` first: single-instance apps (Discord, Telegram, Spotify, most
    Electron apps) just focus their existing window on a repeat launch
    instead of spawning a new process -- treating that as failure would waste
    several seconds waiting for something that will never happen, then fall
    through to slower, more disruptive methods for an app that was already
    open the whole time."""
    stem = app_name.lower().replace(".exe", "").replace(" ", "")
    if not stem:
        return False
    if _process_name_matches(before, stem):
        return True
    deadline = time.time() + timeout
    while True:
        if _process_name_matches(_snapshot_process_names() - before, stem):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.3)


def _find_app_paths_registry(app_name: str) -> str | None:
    """HKCU/HKLM 'App Paths' -- where most Windows installers register their
    main executable, even for apps that never get added to PATH."""
    try:
        import winreg
    except ImportError:
        return None

    names = {app_name, app_name if app_name.lower().endswith(".exe") else app_name + ".exe"}
    for name in names:
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                key = winreg.OpenKey(
                    hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{name}"
                )
                path, _ = winreg.QueryValueEx(key, "")
                winreg.CloseKey(key)
                path = path.strip().strip('"')
                if path and Path(path).exists():
                    return path
            except Exception:
                continue
    return None


def _start_menu_roots() -> list[Path]:
    return [
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    ]


# In-memory index of every Start Menu shortcut, built once (see
# build_app_index(), called from worker.py at startup) instead of walking
# the filesystem on every first lookup of a given app. None until then --
# _find_start_menu_shortcut() falls back to a live scan so nothing breaks
# for callers that run before startup indexing.
_app_index: dict[str, str] | None = None
_app_index_lock = threading.Lock()


def build_app_index() -> int:
    """Scan Start Menu shortcuts once and keep name->path resident in
    memory. Safe to call again later (e.g. after installing something new)
    -- rebuilds from scratch and swaps the index atomically. Returns the
    number of shortcuts indexed."""
    index: dict[str, str] = {}
    for root in _start_menu_roots():
        if not root.exists():
            continue
        try:
            for lnk in root.rglob("*.lnk"):
                index.setdefault(lnk.stem.lower().replace(" ", ""), str(lnk))
        except Exception:
            continue
    global _app_index
    with _app_index_lock:
        _app_index = index
    return len(index)


def _find_start_menu_shortcut(app_name: str) -> str | None:
    """Search Start Menu .lnk shortcuts (per-user + all-users) for a name
    match -- effectively what Windows Search itself resolves against, and it
    covers apps that don't register an App Paths key either (many Electron
    apps: Discord, Spotify, WhatsApp, Telegram...)."""
    search_term = app_name.lower().replace(" ", "")

    if _app_index is not None:
        if search_term in _app_index:
            return _app_index[search_term]
        for stem_norm, path in _app_index.items():
            if search_term in stem_norm or stem_norm in search_term:
                return path
        return None

    best_partial = None
    for root in _start_menu_roots():
        if not root.exists():
            continue
        try:
            for lnk in root.rglob("*.lnk"):
                stem_norm = lnk.stem.lower().replace(" ", "")
                if stem_norm == search_term:
                    return str(lnk)
                if best_partial is None and (search_term in stem_norm or stem_norm in search_term):
                    best_partial = str(lnk)
        except Exception:
            continue
    return best_partial


# Remembers which resolution method worked for an app name last time, so a
# repeat launch skips straight to it instead of re-trying PATH -> registry ->
# Start Menu scan -> Start Menu search from scratch. Cleared the moment a
# cached attempt fails (app moved/uninstalled), so it can never get "stuck".
_resolution_cache: dict[str, tuple[str, str]] = {}


def _launch_via_cache(method: str, target: str, before: set[str], app_name: str) -> bool:
    try:
        if method == "path":
            # shell=False + a list: Windows' CreateProcess still searches PATH for a
            # bare executable name with no path separator, so this resolves exactly
            # like the old shell=True call did -- without ever handing `target`
            # (an LLM/voice-derived string) to cmd.exe for interpretation. See
            # _launch_windows() below for why that distinction matters.
            subprocess.Popen([target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:  # "startfile" -- a resolved App Paths registry value or Start Menu .lnk
            os.startfile(target)  # type: ignore[attr-defined]
        return _wait_for_new_process(before, app_name)
    except Exception:
        return False


def _launch_windows(app_name: str) -> bool:
    before = _snapshot_process_names()
    cache_key = app_name.lower().strip()

    cached = _resolution_cache.get(cache_key)
    if cached:
        if _launch_via_cache(cached[0], cached[1], before, app_name):
            return True
        _resolution_cache.pop(cache_key, None)
        before = _snapshot_process_names()

    if shutil.which(app_name) or shutil.which(app_name.split(".")[0]):
        try:
            # See _launch_via_cache(): no shell=True, so `app_name` (an LLM/voice
            # -derived string) is never parsed by cmd.exe. A crafted name like
            # 'notepad & calc.exe' can't inject a second command this way.
            subprocess.Popen([app_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if _wait_for_new_process(before, app_name):
                _resolution_cache[cache_key] = ("path", app_name)
                return True
        except Exception:
            pass

    reg_path = _find_app_paths_registry(app_name)
    if reg_path:
        try:
            os.startfile(reg_path)  # type: ignore[attr-defined]
            if _wait_for_new_process(before, Path(reg_path).name):
                _resolution_cache[cache_key] = ("startfile", reg_path)
                return True
        except Exception:
            pass

    if ":" in app_name:
        # Was `subprocess.Popen(f"start {app_name}", shell=True)` -- shell=True
        # runs this through cmd.exe, which treats '&', '|', '&&' etc. as command
        # separators. Since this branch's only gate was "contains a colon" (true
        # for any plain drive-letter path AND for a string like 'x & calc.exe'),
        # that was a straight shell-injection hole reachable from the
        # open_application voice/text tool. os.startfile() opens/executes the
        # path exactly the way double-clicking it would, with no shell involved.
        try:
            os.startfile(app_name)  # type: ignore[attr-defined]
            if _wait_for_new_process(before, app_name):
                return True
        except Exception:
            pass

    shortcut = _find_start_menu_shortcut(app_name)
    if shortcut:
        try:
            os.startfile(shortcut)  # type: ignore[attr-defined]
            if _wait_for_new_process(before, Path(shortcut).stem):
                _resolution_cache[cache_key] = ("startfile", shortcut)
                return True
        except Exception:
            pass

    # Last resort: simulate typing into Start Menu search -- slower and less
    # precise, but the widest net: if the OS's own search can find it, so will this.
    try:
        import pyautogui

        pyautogui.PAUSE = 0.1
        pyautogui.press("win")
        time.sleep(0.7)
        pyautogui.write(app_name, interval=0.05)
        time.sleep(0.9)
        pyautogui.press("enter")
        if _wait_for_new_process(before, app_name, timeout=4.0):
            return True
    except Exception:
        pass

    return False


def _launch_macos(app_name: str) -> bool:
    before = _snapshot_process_names()
    try:
        result = subprocess.run(["open", "-a", app_name], capture_output=True, timeout=8)
        if result.returncode == 0 and _wait_for_new_process(before, app_name):
            return True
    except Exception:
        pass
    binary = shutil.which(app_name) or shutil.which(app_name.lower())
    if binary:
        try:
            subprocess.Popen([binary], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if _wait_for_new_process(before, app_name):
                return True
        except Exception:
            pass
    try:
        import pyautogui

        pyautogui.hotkey("command", "space")
        time.sleep(0.6)
        pyautogui.write(app_name, interval=0.05)
        time.sleep(0.8)
        pyautogui.press("enter")
        if _wait_for_new_process(before, app_name, timeout=4.0):
            return True
    except Exception:
        pass
    return False


def _launch_linux(app_name: str) -> bool:
    before = _snapshot_process_names()
    binary = (
        shutil.which(app_name)
        or shutil.which(app_name.lower())
        or shutil.which(app_name.lower().replace(" ", "-"))
    )
    if binary:
        try:
            subprocess.Popen([binary], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if _wait_for_new_process(before, app_name):
                return True
        except Exception:
            pass
    try:
        subprocess.run(["xdg-open", app_name], capture_output=True, timeout=5)
        if _wait_for_new_process(before, app_name):
            return True
    except Exception:
        pass
    return False


_OS_LAUNCHERS = {"Windows": _launch_windows, "Darwin": _launch_macos, "Linux": _launch_linux}


def _launch_sync(app_name: str) -> tuple[bool, str]:
    launcher = _OS_LAUNCHERS.get(_SYSTEM)
    if launcher is None:
        return False, f"Неподдерживаемая ОС: {_SYSTEM}"

    normalized = _normalize(app_name)

    if launcher(normalized):
        return True, f"Открываю {app_name}."
    if normalized.lower() != app_name.lower() and launcher(app_name):
        return True, f"Открываю {app_name}."
    return False, f"Не смог найти и запустить «{app_name}» — возможно, оно не установлено."


async def launch(app_name: str) -> tuple[bool, str]:
    """Resolve and launch `app_name` through every strategy above. Returns
    (success, message). Runs off the event loop since every step here is
    blocking (subprocess, registry, filesystem, psutil polling)."""
    return await asyncio.to_thread(_launch_sync, app_name)
