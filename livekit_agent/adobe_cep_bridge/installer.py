"""Installs the Jarvis CEP extension (extension/) into Adobe's CEP extensions
folder and enables PlayerDebugMode so it's allowed to load unsigned/loose
extensions -- Adobe's own supported mechanism for developing/loading a CEP
panel without going through the paid ZXP signing process. This is the same
approach community tools like pymiere use to drive Premiere Pro/After
Effects from an external process.

What this buys us: once the extension is loaded by a host app (Premiere Pro
/ After Effects), CEP opens a Chrome-DevTools-Protocol debug port for it
(declared in extension/.debug), and any process on this machine can connect
to that port and evaluate JS in the extension's page -- which has access to
the native `window.__adobe_cep__.evalScript()` bridge into the host app's
own ExtendScript engine. See client.py for that half.
"""

from __future__ import annotations

import shutil
import winreg
from pathlib import Path

EXTENSION_ID = "com.jarvis.adobebridge.panel"
_SOURCE_DIR = Path(__file__).resolve().parent / "extension"

# CEP has shipped as CSXS 4 through (at least) 12 across Creative Cloud's
# history; setting PlayerDebugMode for versions this machine doesn't
# actually have is harmless (an unread registry value), so cover a wide
# range rather than trying to detect the exact one each installed app uses.
_CSXS_VERSIONS = [str(v) for v in range(4, 15)]


def _extensions_root() -> Path:
    import os

    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA не найден -- это не Windows?")
    return Path(appdata) / "Adobe" / "CEP" / "extensions"


def is_installed() -> bool:
    target = _extensions_root() / EXTENSION_ID
    return (target / "CSXS" / "manifest.xml").exists()


def install_extension() -> str:
    """Copies extension/ into %APPDATA%/Adobe/CEP/extensions/<id>/. Safe to
    call repeatedly -- always overwrites with the current bundled copy."""
    target = _extensions_root() / EXTENSION_ID
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_SOURCE_DIR, target, dirs_exist_ok=True)
    return f"Расширение установлено: {target}"


def enable_debug_mode() -> list[str]:
    """Sets PlayerDebugMode=1 under HKCU\\Software\\Adobe\\CSXS.<N> for a
    range of CEP runtime versions. Required or Premiere Pro/After Effects
    silently refuse to load an unsigned extension like ours."""
    touched = []
    for version in _CSXS_VERSIONS:
        key_path = f"Software\\Adobe\\CSXS.{version}"
        try:
            key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, "PlayerDebugMode", 0, winreg.REG_SZ, "1")
            winreg.CloseKey(key)
            touched.append(version)
        except OSError:
            continue
    return touched


def ensure_installed() -> str:
    """Idempotent setup: install the extension + enable debug mode. Call
    this before trying to talk to Premiere Pro/After Effects; the app
    itself still needs to be (re)started afterward to pick up a
    newly-installed extension for the first time."""
    install_extension()
    versions = enable_debug_mode()
    return f"CEP-мост установлен (PlayerDebugMode включён для CSXS {', '.join(versions)})."
