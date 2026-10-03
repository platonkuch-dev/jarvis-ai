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
    5. The shell's own app list (Get-StartApps / shell:AppsFolder): the only
       place Store/UWP apps (Paint, Calculator, Notepad on Win11), Steam
       games (steam://rungameid/...) and localized names ("Диспетчер задач")
       show up -- none of them have a Start Menu .lnk.
    6. Last resort, only if that list couldn't be read: type the name into
       the Start Menu search and press Enter.

Web services ("YouTube", "Gmail", "vk.com") aren't programs at all -- they
open in the default browser instead of failing as "not installed".

Every step verifies a real new process actually appeared (via psutil) before
declaring success, and remembers already-open apps so a second "open X"
just focuses them instead of waiting for a process that will never spawn.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import psutil

import config

_SYSTEM = config.SYSTEM


def _contains_words(haystack: str, needle: str) -> bool:
    """Whole-word containment: "google chrome browser" contains "google
    chrome", but "paint.net" does not contain "paint" (a raw substring check
    would turn Paint.NET into mspaint)."""
    hay, words = haystack.split(), needle.split()
    n = len(words)
    return any(hay[i:i + n] == words for i in range(len(hay) - n + 1))


def _normalize(raw: str) -> str:
    key = raw.lower().strip()
    entry = config.APP_ALIASES.get(key)
    if entry is not None:
        return entry.get(_SYSTEM, raw)
    for alias_key, os_map in config.APP_ALIASES.items():
        if _contains_words(key, alias_key):
            return os_map.get(_SYSTEM, raw)
    return raw


def _squash(name: str) -> str:
    """"Counter-Strike 2" / "counter strike" -> "counterstrike2" / "counterstrike"."""
    return re.sub(r"[\W_]+", "", name.lower())


def _web_url(app_name: str) -> str | None:
    """URL for a web service the user called an "app", or None."""
    key = app_name.lower().strip()
    url = config.WEB_APP_URLS.get(key)
    if url:
        return url
    for site, site_url in config.WEB_APP_URLS.items():
        if _contains_words(key, site):
            return site_url
    # "vk.com", "habr.com" -- a bare domain, no spaces, not a local file.
    if re.fullmatch(r"[\w-]+(\.[\w-]+)*\.[a-zа-я]{2,}", key) and not key.endswith(".exe"):
        return "https://" + key
    return None


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
    global _app_index, _start_apps
    with _app_index_lock:
        _app_index = index
    if _SYSTEM == "Windows":
        _start_apps = _load_start_apps()
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


# The shell's own list of launchable apps -- what the Start Menu "All apps"
# view shows: name -> AppUserModelID (or a steam:// URL for Steam games).
# None until loaded; [] if PowerShell couldn't produce it.
_start_apps: list[tuple[str, str, str]] | None = None  # (squashed name, name, app id)


def _load_start_apps() -> list[tuple[str, str, str]]:
    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.decode("utf-8", errors="replace")
        rows = json.loads(out or "[]")
    except Exception:
        return []
    if isinstance(rows, dict):
        rows = [rows]
    apps = []
    for row in rows:
        name, app_id = (row.get("Name") or "").strip(), (row.get("AppID") or "").strip()
        if name and app_id:
            apps.append((_squash(name), name, app_id))
    return apps


def _ensure_start_apps() -> list[tuple[str, str, str]]:
    global _start_apps
    if _start_apps is None:
        _start_apps = _load_start_apps() if _SYSTEM == "Windows" else []
    return _start_apps


def _find_start_app(app_name: str) -> tuple[str, str] | None:
    """(display name, app id) of the best Start-apps match: exact name, then
    a name starting with the query ("counter strike" -> "Counter-Strike 2"),
    then any name containing it. Partial matches need >= 3 letters so "w"
    doesn't launch whatever app sorts first."""
    term = _squash(app_name)
    if not term:
        return None
    apps = _ensure_start_apps()
    for squashed, name, app_id in apps:
        if squashed == term:
            return name, app_id
    if len(term) < 3:
        return None
    for test in (lambda sq: sq.startswith(term), lambda sq: term in sq):
        hits = [(len(sq), name, app_id) for sq, name, app_id in apps if test(sq)]
        if hits:
            _, name, app_id = min(hits)
            return name, app_id
    return None


def _launch_start_app(app_id: str) -> bool:
    """Activate an app by its AppUserModelID exactly as clicking it in Start
    does. Not verified via psutil: Store apps run under names unrelated to
    their title (Calculator -> CalculatorApp.exe) and Steam games start
    after Steam's own delay -- the id comes from the shell's list of
    installed apps, so activation itself is the reliable signal."""
    try:
        if "://" in app_id:
            os.startfile(app_id)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


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
        elif method == "appsfolder":
            return _launch_start_app(target)
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

    # Shell URIs: "ms-settings:", "ms-windows-store:" -- they open a window
    # owned by an unrelated process, so there's nothing to wait for.
    if re.fullmatch(r"[a-z][a-z0-9.+-]+:[^\\/]*", app_name.lower()):
        try:
            os.startfile(app_name)  # type: ignore[attr-defined]
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

    start_app = _find_start_app(app_name)
    if start_app and _launch_start_app(start_app[1]):
        _resolution_cache[cache_key] = ("appsfolder", start_app[1])
        return True

    shortcut = _find_start_menu_shortcut(app_name)
    if shortcut:
        try:
            os.startfile(shortcut)  # type: ignore[attr-defined]
            if _wait_for_new_process(before, Path(shortcut).stem):
                _resolution_cache[cache_key] = ("startfile", shortcut)
                return True
        except Exception:
            pass

    # Last resort: simulate typing into Start Menu search. Only when the
    # shell's app list couldn't be read -- if it could, Start search would
    # find nothing more, and typing a non-app name there just opens a Bing
    # search in Edge (or types into whatever window has focus).
    if _ensure_start_apps():
        return False
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


def _installed_exactly(app_name: str) -> bool:
    term = _squash(app_name)
    return any(sq == term for sq, _, _ in _ensure_start_apps())


_OS_LAUNCHERS = {"Windows": _launch_windows, "Darwin": _launch_macos, "Linux": _launch_linux}


def _launch_sync(app_name: str) -> tuple[bool, str]:
    launcher = _OS_LAUNCHERS.get(_SYSTEM)
    if launcher is None:
        return False, f"Неподдерживаемая ОС: {_SYSTEM}"

    normalized = _normalize(app_name)

    # A web service ("YouTube", "Gmail") -- unless an app by that exact name
    # is installed (a Chrome/Edge PWA), it lives in the browser.
    url = _web_url(app_name)
    if url and not _installed_exactly(app_name):
        import webbrowser

        if webbrowser.open(url):
            return True, f"Открываю {app_name} в браузере."

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
