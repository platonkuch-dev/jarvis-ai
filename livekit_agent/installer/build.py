"""Builds Jarvis AI's Windows installer:  installer/out/JarvisAI-Setup-<version>.exe

Run it with the build environment's Python (the one that has PyInstaller):

    installer\\build_venv\\Scripts\\python.exe installer\\build.py            # everything
    installer\\build_venv\\Scripts\\python.exe installer\\build.py runtime    # one stage
      stages: runtime  app  uninstaller  setup   (each is safe to re-run)

What it produces, in order:
  runtime      python.org's embeddable Python 3.12 + every dependency at the exact versions
               in requirements.lock (the versions that were tested), so the installed program
               needs no Python on the PC and downloads nothing.
  app          Jarvis's own files, chosen the same way as a publication (git's view, honouring
               .gitignore) and scanned for your secrets/personal data first -- the installer
               can never carry your .env, memory, sessions or logs.
  uninstaller  Uninstall.exe (PyInstaller, no console).
  setup        payload.zip appended to a small PyInstaller stub = the single-file Setup.exe.

The build machine needs internet (Python embeddable zip, PyPI, Microsoft's VC++ redistributable).
The installer itself is fully offline.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "scripts"))
import release_check  # noqa: E402  (shared "what would be published" + secret scan)

CACHE = HERE / "cache"
WORK = HERE / "work"
OUT = HERE / "out"
STAGE = WORK / "stage"
VERSION = (HERE / "VERSION").read_text(encoding="utf-8").strip()
PY_VERSION = "3.12.10"
EMBED_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip"
VC_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
LOCK = HERE / "requirements.lock"
ICON = ROOT / "assets" / "jarvis.ico"

# Not shipped inside the installed app: dev tooling, tests, the optional browser client, batch launchers.
EXCLUDE_PREFIXES = ("installer/", "web/", "scripts/", "dist_release/", "docs/")
EXCLUDE_NAMES = {"web_api.py", "test_computer_use.py", "INSTALL_JARVIS.bat", "Start-Panel.bat", ".gitignore"}

# The embeddable interpreter ignores PYTHONPATH and does NOT put a script's own folder on sys.path,
# so the app folder (a sibling of runtime\ in the installed layout) has to be listed here.
PTH = """python312.zip
.
..\\app
Lib\\site-packages
Lib\\site-packages\\win32
Lib\\site-packages\\win32\\lib
Lib\\site-packages\\Pythonwin
import site
"""


def say(msg: str) -> None:
    print(f"==> {msg}", flush=True)


def fetch(url: str, name: str) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    dest = CACHE / name
    if dest.exists() and dest.stat().st_size > 1_000_000:
        return dest
    say(f"скачиваю {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=60) as resp, tmp.open("wb") as out:
        shutil.copyfileobj(resp, out)
    tmp.replace(dest)
    return dest


def pyinstaller(script: Path, name: str, extra: list[str] | None = None) -> Path:
    dist = WORK / "dist"
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--noconsole", "--name", name,
        "--icon", str(ICON), "--distpath", str(dist), "--workpath", str(WORK / "pyi" / name),
        "--specpath", str(WORK / "pyi"), "--paths", str(HERE), "--add-data", f"{ICON};.", *(extra or []), str(script),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
    return dist / f"{name}.exe"


# ---------------------------------------------------------------------------


# Qt modules the HUD never imports (it uses QtCore/QtGui/QtWidgets only): safe to drop, ~80 MB.
QT_DROP_DIRS = ("qml", "translations", "qsci")
QT_DROP_DLL_PREFIXES = ("Qt6Quick", "Qt6Qml", "Qt6Pdf", "Qt6Designer", "Qt6ShaderTools", "Qt6Multimedia",
                        "avcodec", "avformat", "avutil", "swresample", "swscale")


def finalize_runtime(runtime: Path) -> None:
    """Idempotent post-processing: trim unused Qt parts, then byte-compile so the first launch isn't slow."""
    (runtime / "python312._pth").write_text(PTH, encoding="ascii")
    qt = runtime / "Lib" / "site-packages" / "PyQt6" / "Qt6"
    for name in QT_DROP_DIRS:
        shutil.rmtree(qt / name, ignore_errors=True)
    for dll in (qt / "bin").glob("*.dll"):
        if dll.name.startswith(QT_DROP_DLL_PREFIXES):
            dll.unlink()
    say("компилирую байткод")
    subprocess.run(
        [str(runtime / "python.exe"), "-m", "compileall", "-q", "-j", "0", str(runtime / "Lib" / "site-packages")],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def stage_runtime() -> None:
    if sys.version_info[:3] != tuple(int(x) for x in PY_VERSION.split(".")):
        sys.exit(f"Сборка должна идти под Python {PY_VERSION} (тот же, что во встроенном рантайме), а сейчас {sys.version.split()[0]}.")
    runtime = STAGE / "runtime"
    stamp = runtime / ".build-stamp"
    want = hashlib.sha256((LOCK.read_bytes() + PY_VERSION.encode())).hexdigest()
    if stamp.exists() and stamp.read_text() == want:
        say("runtime уже собран под этот requirements.lock — только дообработка")
        finalize_runtime(runtime)
        return
    shutil.rmtree(runtime, ignore_errors=True)
    runtime.mkdir(parents=True)

    say(f"встраиваемый Python {PY_VERSION}")
    with zipfile.ZipFile(fetch(EMBED_URL, f"python-{PY_VERSION}-embed-amd64.zip")) as z:
        z.extractall(runtime)
    (runtime / "python312._pth").write_text(PTH, encoding="ascii")

    site = runtime / "Lib" / "site-packages"
    site.mkdir(parents=True)
    say("устанавливаю зависимости (несколько минут)")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--target", str(site), "--no-warn-script-location",
         "--disable-pip-version-check", "--no-compile", "-r", str(LOCK)],
        check=True,
    )

    say("чищу лишнее")
    shutil.rmtree(site / "bin", ignore_errors=True)
    for cache in site.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    # pywin32's DLLs must sit where the interpreter finds them without running pywin32's post-install step.
    for dll in (site / "pywin32_system32").glob("*.dll"):
        shutil.copy2(dll, runtime / dll.name)
    finalize_runtime(runtime)
    stamp.write_text(want)
    say(f"runtime: {sum(f.stat().st_size for f in runtime.rglob('*') if f.is_file()) // 2**20} МБ")


def stage_app() -> None:
    app = STAGE / "app"
    shutil.rmtree(app, ignore_errors=True)
    files = []
    for path in release_check.published_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(EXCLUDE_PREFIXES) or path.name in EXCLUDE_NAMES or (path.name.startswith("test_") and path.suffix == ".py"):
            continue
        files.append(path)

    errors, _warnings = release_check.scan(files)
    if errors:
        print("\n".join("  ✗ " + e for e in errors))
        sys.exit("В файлах приложения найдены секреты или личные данные — установщик не собран.")

    for path in files:
        target = app / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    say(f"app: {len(files)} файлов, проверено на секреты")


def stage_uninstaller() -> None:
    exe = pyinstaller(HERE / "uninstall_stub.py", "Uninstall")
    say(f"Uninstall.exe: {exe.stat().st_size // 2**20} МБ")


def stage_setup() -> None:
    uninstaller = WORK / "dist" / "Uninstall.exe"
    if not uninstaller.exists():
        stage_uninstaller()
    vc = fetch(VC_URL, "vc_redist.x64.exe")

    payload = WORK / "payload.zip"
    say("собираю payload.zip (несколько минут)")
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("manifest.json", json.dumps({"name": "Jarvis AI", "version": VERSION}))
        z.write(uninstaller, "Uninstall.exe")
        z.write(ICON, "jarvis.ico")
        z.write(vc, "redist/vc_redist.x64.exe")
        for folder in ("runtime", "app"):
            base = STAGE / folder
            for path in sorted(base.rglob("*")):
                if path.is_file() and path.name != ".build-stamp":
                    z.write(path, f"{folder}/{path.relative_to(base).as_posix()}")

    stub = pyinstaller(HERE / "setup_stub.py", "Setup")
    OUT.mkdir(exist_ok=True)
    target = OUT / f"JarvisAI-Setup-{VERSION}.exe"
    say("склеиваю Setup.exe")
    with target.open("wb") as out:
        for part in (stub, payload):
            with part.open("rb") as src:
                shutil.copyfileobj(src, out, 1 << 22)
    sha = hashlib.sha256()
    with target.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            sha.update(chunk)
    (OUT / f"JarvisAI-Setup-{VERSION}.sha256").write_text(f"{sha.hexdigest()}  {target.name}\n")
    say(f"готово: {target}  ({target.stat().st_size // 2**20} МБ)\n    sha256 {sha.hexdigest()}")


STAGES = {"runtime": stage_runtime, "app": stage_app, "uninstaller": stage_uninstaller, "setup": stage_setup}


def main() -> None:
    wanted = sys.argv[1:] or ["runtime", "app", "uninstaller", "setup"]
    for name in wanted:
        if name not in STAGES:
            sys.exit(f"Неизвестный этап «{name}». Доступны: {', '.join(STAGES)}")
    for name in wanted:
        STAGES[name]()


if __name__ == "__main__":
    main()
