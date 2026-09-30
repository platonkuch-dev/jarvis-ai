import os
import time
import subprocess
import platform
import shutil
import threading
import webbrowser
from pathlib import Path

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

_SYSTEM = platform.system()

_APP_ALIASES: dict[str, dict[str, str]] = {

    "chrome":             {"Windows": "chrome",                  "Darwin": "Google Chrome",        "Linux": "google-chrome"},
    "google chrome":      {"Windows": "chrome",                  "Darwin": "Google Chrome",        "Linux": "google-chrome"},
    "firefox":            {"Windows": "firefox",                 "Darwin": "Firefox",              "Linux": "firefox"},
    "edge":               {"Windows": "msedge",                  "Darwin": "Microsoft Edge",       "Linux": "microsoft-edge"},
    "brave":              {"Windows": "brave",                   "Darwin": "Brave Browser",        "Linux": "brave-browser"},
    "safari":             {"Windows": "msedge",                  "Darwin": "Safari",               "Linux": "firefox"},
    "opera":              {"Windows": "opera",                   "Darwin": "Opera",                "Linux": "opera"},
    "whatsapp":           {"Windows": "WhatsApp",                "Darwin": "WhatsApp",             "Linux": "whatsapp"},
    "telegram":           {"Windows": "Telegram",                "Darwin": "Telegram",             "Linux": "telegram"},
    "discord":            {"Windows": "Discord",                 "Darwin": "Discord",              "Linux": "discord"},
    "slack":              {"Windows": "Slack",                   "Darwin": "Slack",                "Linux": "slack"},
    "zoom":               {"Windows": "Zoom",                    "Darwin": "zoom.us",              "Linux": "zoom"},
    "teams":              {"Windows": "msteams",                 "Darwin": "Microsoft Teams",      "Linux": "teams"},
    "skype":              {"Windows": "skype",                   "Darwin": "Skype",                "Linux": "skype"},
    "signal":             {"Windows": "signal",                  "Darwin": "Signal",               "Linux": "signal"},
    "spotify":            {"Windows": "Spotify",                 "Darwin": "Spotify",              "Linux": "spotify"},
    "vlc":                {"Windows": "vlc",                     "Darwin": "VLC",                  "Linux": "vlc"},
    "netflix":            {"Windows": "Netflix",                 "Darwin": "Netflix",              "Linux": "firefox"},
    "vscode":             {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "visual studio code": {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "code":               {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "terminal":           {"Windows": "wt",                      "Darwin": "Terminal",             "Linux": "gnome-terminal"},
    "cmd":                {"Windows": "cmd.exe",                 "Darwin": "Terminal",             "Linux": "bash"},
    "powershell":         {"Windows": "powershell.exe",          "Darwin": "Terminal",             "Linux": "bash"},
    "postman":            {"Windows": "Postman",                 "Darwin": "Postman",              "Linux": "postman"},
    "git":                {"Windows": "git-bash",                "Darwin": "Terminal",             "Linux": "bash"},
    "figma":              {"Windows": "Figma",                   "Darwin": "Figma",                "Linux": "figma"},
    "blender":            {"Windows": "blender",                 "Darwin": "Blender",              "Linux": "blender"},
    "word":               {"Windows": "winword",                 "Darwin": "Microsoft Word",       "Linux": "libreoffice --writer"},
    "excel":              {"Windows": "excel",                   "Darwin": "Microsoft Excel",      "Linux": "libreoffice --calc"},
    "powerpoint":         {"Windows": "powerpnt",                "Darwin": "Microsoft PowerPoint", "Linux": "libreoffice --impress"},
    "libreoffice":        {"Windows": "soffice",                 "Darwin": "LibreOffice",          "Linux": "libreoffice"},
    "notepad":            {"Windows": "notepad.exe",             "Darwin": "TextEdit",             "Linux": "gedit"},
    "textedit":           {"Windows": "notepad.exe",             "Darwin": "TextEdit",             "Linux": "gedit"},
    "explorer":           {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "file explorer":      {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "finder":             {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "task manager":       {"Windows": "taskmgr.exe",             "Darwin": "Activity Monitor",     "Linux": "gnome-system-monitor"},
    "settings":           {"Windows": "ms-settings:",            "Darwin": "System Preferences",   "Linux": "gnome-control-center"},
    "calculator":         {"Windows": "calc.exe",                "Darwin": "Calculator",           "Linux": "gnome-calculator"},
    "paint":              {"Windows": "mspaint.exe",             "Darwin": "Preview",              "Linux": "gimp"},
    "instagram":          {"Windows": "Instagram",               "Darwin": "Instagram",            "Linux": "firefox"},
    "tiktok":             {"Windows": "TikTok",                  "Darwin": "TikTok",               "Linux": "firefox"},
    "notion":             {"Windows": "Notion",                  "Darwin": "Notion",               "Linux": "notion"},
    "obsidian":           {"Windows": "Obsidian",                "Darwin": "Obsidian",             "Linux": "obsidian"},
    "capcut":             {"Windows": "CapCut",                  "Darwin": "CapCut",               "Linux": "capcut"},
    "steam":              {"Windows": "steam",                   "Darwin": "Steam",                "Linux": "steam"},
    "epic":               {"Windows": "EpicGamesLauncher",       "Darwin": "Epic Games Launcher",  "Linux": "legendary"},
    "epic games":         {"Windows": "EpicGamesLauncher",       "Darwin": "Epic Games Launcher",  "Linux": "legendary"},
}

_BROWSER_START_PAGES = {
    "chrome": "https://www.google.com",
    "firefox": "https://www.google.com",
    "msedge": "https://www.google.com",
    "brave": "https://www.google.com",
    "opera": "https://www.google.com",
}

_WEBSITE_ALIASES = {
    "google": "https://www.google.com",
    "youtube": "https://www.youtube.com",
    "gmail": "https://mail.google.com",
    "instagram": "https://www.instagram.com",
    "facebook": "https://www.facebook.com",
    "twitter": "https://x.com",
    "x": "https://x.com",
    "tiktok": "https://www.tiktok.com",
    "reddit": "https://www.reddit.com",
    "wikipedia": "https://www.wikipedia.org",
}


def _normalize(raw: str) -> str:
    key = raw.lower().strip()

    if key in _APP_ALIASES:
        return _APP_ALIASES[key].get(_SYSTEM, raw)

    for alias_key, os_map in _APP_ALIASES.items():
        if alias_key in key or key in alias_key:
            return os_map.get(_SYSTEM, raw)

    return raw  


def _website_url(raw: str) -> str | None:
    value = raw.strip().lower()
    if value in _WEBSITE_ALIASES:
        return _WEBSITE_ALIASES[value]
    if value.startswith(("http://", "https://")):
        return raw.strip()
    if "." in value and " " not in value:
        return "https://" + raw.strip()
    return None


def _open_browser_start_page(browser: str) -> bool:
    """Launch a requested browser with a useful initial page, never about:blank."""
    url = _BROWSER_START_PAGES[browser]
    if _SYSTEM == "Windows":
        executable = shutil.which(browser) or _find_app_paths_registry(browser)
        if executable:
            try:
                subprocess.Popen([executable, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except Exception as error:
                print(f"[open_app] Browser launch failed: {error}")
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception as error:
        print(f"[open_app] Browser start page failed: {error}")
        return False

def _snapshot_process_names() -> set[str]:
    if not _PSUTIL:
        return set()
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
    """Poll for up to `timeout` seconds for a process whose name plausibly
    matches app_name to be running. Without psutil we can't verify anything,
    so a launch call that didn't raise is treated as good enough.

    Checks `before` first: single-instance apps (Discord, Telegram, Spotify,
    most Electron apps) just focus their EXISTING window on a repeat launch
    instead of spawning a new process — treating that as "no new process
    appeared, so the launch failed" wastes several seconds waiting for
    something that will never happen, then falls through to slower, more
    disruptive fallback methods (down to typing into the Start Menu search)
    for an app that was already open the whole time. Measured live: a real
    "open Discord" call took 9.7s and still reported failure this way,
    right before this fix, with Discord already running since earlier in
    the session."""
    if not _PSUTIL:
        return True
    stem = app_name.lower().replace(".exe", "").replace(" ", "")
    if not stem:
        return False
    if _process_name_matches(before, stem):
        return True  # already running — this launch call just focused it
    deadline = time.time() + timeout
    while True:
        if _process_name_matches(_snapshot_process_names() - before, stem):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.3)


def _find_app_paths_registry(app_name: str) -> str | None:
    """Look up HKCU/HKLM 'App Paths' — where most Windows installers register
    their main executable, even for apps that never get added to PATH."""
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
        Path(os.environ.get("APPDATA", ""))     / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    ]


# Application Registry, part 2 — an in-memory index of every Start Menu
# shortcut, built ONCE at startup (see build_app_index(), called from
# main.py) instead of walking the filesystem on every *first* lookup of a
# given app name. _resolution_cache above only speeds up a REPEAT launch of
# the same app; this is what removes the "search Discord on disk" cost from
# the very first launch too, per the project's own "не должен каждый раз
# искать приложение по диску" requirement. None until build_app_index()
# runs; _find_start_menu_shortcut() falls back to a live scan until then, so
# nothing breaks for callers (tests, the demo script) that skip startup.
_app_index: dict[str, str] | None = None
_app_index_lock = threading.Lock()


def build_app_index() -> int:
    """Scan Start Menu shortcuts once and keep name->path resident in
    memory. Safe to call again later (e.g. after installing something new)
    — rebuilds from scratch and swaps the index atomically. Returns the
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
    match. This is effectively what Windows Search itself resolves against,
    and it covers apps that don't register an App Paths key either (many
    Electron apps: Discord, Spotify, WhatsApp, Telegram…)."""
    search_term = app_name.lower().replace(" ", "")

    if _app_index is not None:
        if search_term in _app_index:
            return _app_index[search_term]
        best_partial = None
        for stem_norm, path in _app_index.items():
            if search_term in stem_norm or stem_norm in search_term:
                best_partial = path
                break
        return best_partial

    # Index not built yet (startup scan hasn't run, or a standalone caller
    # like fast_path_demo.py never triggers it) — fall back to a live scan
    # so this still works correctly, just without the startup speedup.
    best_partial = None
    for root in _start_menu_roots():
        if not root.exists():
            continue
        try:
            for lnk in root.rglob("*.lnk"):
                stem_norm = lnk.stem.lower().replace(" ", "")
                if stem_norm == search_term:
                    return str(lnk)  # exact match — best possible
                if best_partial is None and (search_term in stem_norm or stem_norm in search_term):
                    best_partial = str(lnk)
        except Exception:
            continue
    return best_partial


# Application Registry — remembers which resolution method actually worked
# for an app name last time (PATH string, or a resolved App Paths/Start Menu
# target launched via os.startfile), so a repeat launch of the same app
# skips straight to it instead of re-trying PATH -> registry -> Start Menu
# shortcut scan -> Start Menu search from scratch. Cleared automatically for
# an entry the moment a cached attempt fails (app moved/uninstalled), so a
# stale cache can never get "stuck" — it just falls back to full resolution.
_resolution_cache: dict[str, tuple[str, str]] = {}


def _launch_via_cache(method: str, target: str, before: set[str], app_name: str) -> bool:
    try:
        if method == "path":
            subprocess.Popen(target, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:  # "startfile" — a resolved App Paths registry value or Start Menu .lnk
            os.startfile(target)
        return _wait_for_new_process(before, app_name)
    except Exception as e:
        print(f"[open_app] Cached resolution for '{app_name}' failed ({e}) — re-resolving")
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

    # PATH first — this is already correct and fast for the built-in tools that
    # register themselves there (notepad, calc, explorer, cmd, powershell, code…).
    if shutil.which(app_name) or shutil.which(app_name.split(".")[0]):
        try:
            subprocess.Popen(
                app_name,
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if _wait_for_new_process(before, app_name):
                _resolution_cache[cache_key] = ("path", app_name)
                return True
            print(f"[open_app] PATH launch of '{app_name}' didn't produce a process — trying next method")
        except Exception as e:
            print(f"[open_app] subprocess failed: {e}")

    # Most consumer apps (Chrome, Discord, Spotify, WhatsApp…) never get added to
    # PATH by their installers — this is where they actually get found.
    reg_path = _find_app_paths_registry(app_name)
    if reg_path:
        try:
            os.startfile(reg_path)
            if _wait_for_new_process(before, Path(reg_path).name):
                _resolution_cache[cache_key] = ("startfile", reg_path)
                return True
            print(f"[open_app] App Paths launch of '{app_name}' didn't produce a process — trying next method")
        except Exception as e:
            print(f"[open_app] App Paths launch failed: {e}")

    if ":" in app_name:
        try:
            subprocess.Popen(f"start {app_name}", shell=True)
            if _wait_for_new_process(before, app_name):
                return True
        except Exception:
            pass

    shortcut = _find_start_menu_shortcut(app_name)
    if shortcut:
        try:
            os.startfile(shortcut)
            if _wait_for_new_process(before, Path(shortcut).stem):
                _resolution_cache[cache_key] = ("startfile", shortcut)
                return True
            print(f"[open_app] Start Menu shortcut launch of '{app_name}' didn't produce a process — trying next method")
        except Exception as e:
            print(f"[open_app] Start Menu shortcut launch failed: {e}")

    # Last resort: simulate typing into Start Menu search. Slower and less
    # precise than the methods above, but it's the widest net — if the OS's
    # own search can find it by name, this will too.
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
        print(f"[open_app] Start Menu search for '{app_name}' didn't produce a process")
    except Exception as e:
        print(f"[open_app] Start Menu search failed: {e}")

    return False


def _launch_macos(app_name: str) -> bool:
    before = _snapshot_process_names()

    try:
        result = subprocess.run(
            ["open", "-a", app_name],
            capture_output=True, timeout=8
        )
        if result.returncode == 0 and _wait_for_new_process(before, app_name):
            return True
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["open", "-a", f"{app_name}.app"],
            capture_output=True, timeout=8
        )
        if result.returncode == 0 and _wait_for_new_process(before, app_name):
            return True
    except Exception:
        pass

    binary = shutil.which(app_name) or shutil.which(app_name.lower())
    if binary:
        try:
            subprocess.Popen(
                [binary],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
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
    except Exception as e:
        print(f"[open_app] Spotlight failed: {e}")

    return False


def _launch_linux(app_name: str) -> bool:
    before = _snapshot_process_names()

    binary = (
        shutil.which(app_name) or
        shutil.which(app_name.lower()) or
        shutil.which(app_name.lower().replace(" ", "-")) or
        shutil.which(app_name.lower().replace(" ", "_"))
    )
    if binary:
        try:
            subprocess.Popen(
                [binary],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            if _wait_for_new_process(before, app_name):
                return True
        except Exception:
            pass

    try:
        subprocess.run(
            ["xdg-open", app_name],
            capture_output=True, timeout=5
        )
        if _wait_for_new_process(before, app_name):
            return True
    except Exception:
        pass

    for desktop_name in [
        app_name.lower(),
        app_name.lower().replace(" ", "-"),
        app_name.lower().replace(" ", ""),
    ]:
        try:
            result = subprocess.run(
                ["gtk-launch", desktop_name],
                capture_output=True, timeout=5
            )
            if result.returncode == 0 and _wait_for_new_process(before, app_name):
                return True
        except Exception:
            pass

    return False


_OS_LAUNCHERS = {
    "Windows": _launch_windows,
    "Darwin":  _launch_macos,
    "Linux":   _launch_linux,
}

def open_app(
    parameters=None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    app_name = (parameters or {}).get("app_name", "").strip()

    if not app_name:
        return "No application name provided."

    website = _website_url(app_name)
    if website:
        try:
            if webbrowser.open(website, new=2):
                return f"Opened {website}."
        except Exception as error:
            print(f"[open_app] Website launch failed: {error}")
        return f"Failed to open {website}."

    launcher = _OS_LAUNCHERS.get(_SYSTEM)
    if launcher is None:
        return f"Unsupported operating system: {_SYSTEM}"

    normalized = _normalize(app_name)
    print(f"[open_app] Launching: '{app_name}' → '{normalized}' ({_SYSTEM})")

    if player:
        player.write_log(f"[open_app] {app_name}")

    try:
        if normalized.lower() in _BROWSER_START_PAGES:
            if _open_browser_start_page(normalized.lower()):
                return f"Opened {app_name} at Google."
            return f"Failed to open {app_name}."
        if launcher(normalized):
            return f"Opened {app_name}."
        if normalized.lower() != app_name.lower():
            if launcher(app_name):
                return f"Opened {app_name}."
        return (
            f"Could not confirm that {app_name} launched. "
            f"It may still be loading, or it might not be installed."
        )
    except Exception as e:
        print(f"[open_app] Error: {e}")
        return f"Failed to open {app_name}: {e}"