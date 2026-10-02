"""After Claude Code changes Jarvis's own code: build, publish, commit, install.

Started by tools/coding_agent.py as a detached process (the install at the end
closes the Jarvis that started it), from the source tree's own venv:

    .venv\\Scripts\\python.exe scripts\\self_release.py --job job.json

job.json: {"task": what was asked, "summary": Claude Code's result,
           "outbox": the installed app's data/notify_outbox.jsonl,
           "installed_env": the installed app's .env (scanned for secrets)}

Steps, each reported to the owner's Telegram through the outbox (the monitor
delivers it, also across the restart):
  1. bump installer/VERSION (patch)          4. GitHub release vX.Y.Z (older ones stay)
  2. build installer/out/JarvisAI-Setup-*.exe 5. silent install over the current one + check
  3. secret scan of the staged livekit_agent/ changes, commit, push
A failed build undoes the version bump and stops before anything is
published. Log: logs/self_release.log.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # livekit_agent/
REPO = ROOT.parent                                      # git repository root
INSTALLER = ROOT / "installer"
VERSION_FILE = INSTALLER / "VERSION"
BUILD_PY = INSTALLER / "build_venv" / "Scripts" / "python.exe"
GITHUB_REPO = "platonkuch-dev/jarvis-ai"
INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Jarvis AI"
LOCK_FILE = ROOT / "logs" / "self_release.lock"
NO_WINDOW = 0x08000000

sys.path.insert(0, str(ROOT / "scripts"))
import release_check  # noqa: E402

logger = logging.getLogger("self_release")


class Failed(Exception):
    pass


def run(args: list[str], *, cwd: Path = REPO, timeout: float = 600, check: bool = True,
        env: dict[str, str] | None = None) -> str:
    logger.info("$ %s", " ".join(args))
    proc = subprocess.run(args, cwd=str(cwd), capture_output=True, timeout=timeout, creationflags=NO_WINDOW,
                          encoding="utf-8", errors="replace", env=env)
    out = (proc.stdout or "") + (proc.stderr or "")
    logger.info(out[-4000:])
    if check and proc.returncode != 0:
        raise Failed(f"{Path(args[0]).name} {' '.join(args[1:3])}: код {proc.returncode}. {out.strip()[-400:]}")
    return proc.stdout or ""


def find_gh() -> str:
    for candidate in (os.environ.get("GH_EXE"), shutil.which("gh"),
                      Path(os.environ.get("ProgramFiles", "")) / "GitHub CLI" / "gh.exe",
                      Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "GitHub CLI" / "gh.exe"):
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise Failed("не нашёл GitHub CLI (gh) — без него не могу запушить и выложить релиз")


def gh_env(gh: str) -> dict[str, str]:
    """Environment for `gh` with GH_TOKEN set. This script runs on the
    Microsoft Store Python, whose child processes see a virtualized %APPDATA%
    without gh's hosts.yml, so plain `gh release` says "not logged in" -- but
    `gh auth git-credential` still reads the token from the Windows keyring.
    The token stays in memory: never logged, never written."""
    proc = subprocess.run([gh, "auth", "git-credential", "get"], input="protocol=https\nhost=github.com\n\n",
                          capture_output=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
                          timeout=60)
    token = next((line.split("=", 1)[1] for line in proc.stdout.splitlines() if line.startswith("password=")), "")
    if not token:
        raise Failed("GitHub CLI не отдал токен — войдите заново командой gh auth login")
    return dict(os.environ, GH_TOKEN=token)


class Notifier:
    def __init__(self, outbox: str | None) -> None:
        self.outbox = Path(outbox) if outbox else None

    def __call__(self, text: str) -> None:
        logger.info("notify: %s", text)
        if self.outbox is None:
            return
        entry = {"id": uuid.uuid4().hex[:10], "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": "task",
                 "text": text}
        try:
            with open(self.outbox, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            logger.warning("could not write to the outbox", exc_info=True)


def bump(version: str) -> str:
    parts = version.strip().split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def secret_check(installed_env: str | None) -> None:
    """release_check over the staged files, plus the installed app's own .env
    values (the source tree's .env may hold different keys)."""
    names = run(["git", "diff", "--cached", "--name-only", "-z"]).split("\0")
    files = [REPO / n for n in names if n and (REPO / n).is_file()]
    files = [f for f in files if ROOT in f.parents]
    errors, _warnings = release_check.scan(files)
    extra: dict[str, str] = {}
    if installed_env and Path(installed_env).is_file():
        for line in Path(installed_env).read_text(encoding="utf-8", errors="ignore").splitlines():
            m = re.match(r"\s*([A-Z0-9_]+)\s*=\s*(.*)$", line)
            if m and release_check.SECRET_NAME.search(m.group(1)):
                value = m.group(2).strip().strip("\"'")
                if len(value) >= 6:
                    extra[m.group(1)] = value
    for path in files:
        if path.suffix.lower() in release_check.SKIP_SCAN_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        errors += [f"{path.relative_to(REPO).as_posix()}: содержит секрет {k} из .env" for k, v in extra.items()
                   if v in text]
    if errors:
        raise Failed("в коммит попали бы секреты или личные данные: " + "; ".join(errors[:5]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    job = json.loads(Path(args.job).read_text(encoding="utf-8"))
    notify = Notifier(job.get("outbox"))
    task = (job.get("task") or "").strip()
    summary = (job.get("summary") or "").strip()

    (ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(filename=ROOT / "logs" / "self_release.log", level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        if time.time() - LOCK_FILE.stat().st_mtime < 3 * 3600:
            notify("🛠 Сборка новой версии уже идёт — эту пропускаю, изменения войдут в следующую.")
            return 1
        LOCK_FILE.unlink(missing_ok=True)
        return main()

    old_version = VERSION_FILE.read_text(encoding="utf-8").strip()
    version = bump(old_version)
    stage = "подготовка"
    try:
        if not run(["git", "status", "--porcelain", "--", str(ROOT)]).strip():
            notify("🛠 Claude Code ничего не изменил в коде Джарвиса — собирать нечего.")
            return 0
        gh = find_gh()

        stage = "сборка установщика"
        notify(f"🛠 Код Джарвиса изменён. Собираю установщик {version} (несколько минут)…")
        VERSION_FILE.write_text(version, encoding="utf-8")
        try:
            run([str(BUILD_PY), str(INSTALLER / "build.py")], cwd=ROOT, timeout=3600)
        except Exception:
            VERSION_FILE.write_text(old_version, encoding="utf-8")
            raise
        setup = INSTALLER / "out" / f"JarvisAI-Setup-{version}.exe"
        if not setup.is_file():
            VERSION_FILE.write_text(old_version, encoding="utf-8")
            raise Failed(f"сборка не создала {setup.name}")

        stage = "коммит"
        run(["git", "add", "-A", "--", str(ROOT)])
        secret_check(job.get("installed_env"))
        title = re.sub(r"\s+", " ", task)[:72] or "изменения от Claude Code"
        message = (f"feat(livekit_agent): {title}\n\nВерсия {version}. Сделано Claude Code по просьбе через Джарвиса.\n\n"
                   f"Задача: {task[:1500]}\n")
        run(["git", "commit", "-q", "-m", message])
        sha = run(["git", "rev-parse", "HEAD"]).strip()

        stage = "отправка на GitHub"
        helper = "!\"" + gh.replace("\\", "/") + "\" auth git-credential"
        run(["git", "-c", "credential.helper=", "-c", f"credential.helper={helper}", "push", "origin", "HEAD"],
            timeout=600)

        stage = "релиз на GitHub"
        notes = (f"{title}\n\n{summary[:3000]}\n\nСобрано автоматически после работы Claude Code. "
                 "Установщик полностью офлайн.")
        notes_file = ROOT / "logs" / f"release_notes_{version}.md"
        notes_file.write_text(notes, encoding="utf-8")
        run([gh, "release", "create", f"v{version}", str(setup), str(setup.with_suffix(".sha256")),
             "-R", GITHUB_REPO, "--title", f"Jarvis AI {version}", "--notes-file", str(notes_file),
             "--target", sha, "--latest"], timeout=1800, env=gh_env(gh))
        notify(f"🛠 Версия {version} выложена на GitHub и закоммичена. Устанавливаю — Джарвис перезапустится.")

        stage = "установка"
        # Only the setup process is waited for -- the Jarvis it launches keeps running.
        proc = subprocess.Popen([str(setup), "/S", "/AUTOSTART", "/LAUNCH"], creationflags=NO_WINDOW,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        code = proc.wait(timeout=1800)
        installed = (INSTALL_DIR / ".jarvisai-install").read_text(encoding="utf-8").strip() \
            if (INSTALL_DIR / ".jarvisai-install").exists() else "?"
        if code != 0 or installed != version:
            raise Failed(f"установщик вернул код {code}, установлена версия {installed}")
        notify(f"✅ Джарвис {version} установлен и запущен. Изменение: {title}")
        return 0
    except Exception as exc:
        logger.exception("self release failed at %s", stage)
        notify(f"⚠️ Новая версия не вышла — сбой на этапе «{stage}»: {exc}. Подробности в logs/self_release.log.")
        return 1
    finally:
        LOCK_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
