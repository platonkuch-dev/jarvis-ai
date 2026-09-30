"""Pre-publication safety check: run this before every push or sale.

    python scripts/release_check.py            # scan only
    python scripts/release_check.py --zip      # scan, and if clean build dist_release/jarvis-<date>.zip

It works out which files would actually be published (git's view: tracked or
untracked-but-not-ignored, so .gitignore is honoured), then looks inside them for
* the literal secret values sitting in YOUR local .env (API keys, Telegram hash, ...),
* your personal data from data/ (name, city, phone numbers found in memory),
* well-known key formats (Anthropic, OpenAI-style, AWS, private keys),
* files that must never ship (.env, *.session, data/, logs/),
* absolute paths from your machine,
and confirms every registered tool is described in capabilities.py.

Exit code 0 = clean, 1 = something to fix. Nothing is ever sent anywhere.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXT_LIMIT = 5 * 1024 * 1024
SKIP_SCAN_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".woff", ".woff2", ".ttf", ".zip", ".pyc", ".lock", ".exe", ".dll", ".onnx"}
NEVER_SHIP = re.compile(r"(^|/)(\.env(\..+)?|.*\.session(-journal)?|data/.*|logs/.*)$")
SECRET_NAME = re.compile(r"(KEY|SECRET|HASH|TOKEN|PASSWORD|PHONE|API_ID)", re.I)
KEY_PATTERNS = {
    "Anthropic API key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
LOCAL_PATH = re.compile(r"[A-Za-z]:\\(?:Users\\[^\\\s\"']+|джарвисы)", re.I)
PHONE = re.compile(r"\+?\d[\d\s\-()]{9,}\d")


def published_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT, capture_output=True, check=True,
        ).stdout.decode("utf-8", "replace")
        names = [n for n in out.split("\0") if n]
        if names:
            return sorted({ROOT / n for n in names if (ROOT / n).is_file()})
    except (OSError, subprocess.CalledProcessError):
        pass
    print("! git не найден или не репозиторий — проверяю по упрощённым правилам (без .gitignore).")
    skip = {".venv", "data", "logs", "__pycache__", ".git", "node_modules", "dist_release", ".claude"}
    return sorted(p for p in ROOT.rglob("*") if p.is_file() and not (set(p.relative_to(ROOT).parts) & skip))


def local_secrets() -> dict[str, str]:
    """Literal values of the sensitive keys in this machine's .env."""
    found: dict[str, str] = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = re.match(r"\s*([A-Z0-9_]+)\s*=\s*(.*)$", line)
            if not m:
                continue
            key, value = m.group(1), m.group(2).strip().strip("\"'")
            if SECRET_NAME.search(key) and len(value) >= 6:
                found[f".env:{key}"] = value
    token = ROOT / "data" / "panel_token.txt"
    if token.exists():
        found["data/panel_token.txt"] = token.read_text(encoding="utf-8", errors="ignore").strip()
    return {k: v for k, v in found.items() if v}


def personal_strings() -> dict[str, str]:
    """Things from your own data that must not appear in published code."""
    found: dict[str, str] = {}
    memory = ROOT / "data" / "memory.json"
    if memory.exists():
        try:
            data = json.loads(memory.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
        for key in ("name", "city"):
            entry = (data.get("identity") or {}).get(key)
            value = entry.get("value") if isinstance(entry, dict) else entry
            if isinstance(value, str) and len(value) >= 4:
                found[f"memory identity.{key}"] = value
        for cat in data.values():
            if not isinstance(cat, dict):
                continue
            for entry in cat.values():
                value = entry.get("value") if isinstance(entry, dict) else entry
                for m in PHONE.finditer(value if isinstance(value, str) else ""):
                    digits = re.sub(r"\D", "", m.group())
                    if len(digits) >= 10:
                        found[f"memory phone {digits[-4:]}"] = digits
    presets = ROOT / "data" / "voice_presets.json"
    if presets.exists():
        try:
            for name, voice_id in json.loads(presets.read_text(encoding="utf-8")).items():
                found[f"voice id ({name})"] = str(voice_id)
        except ValueError:
            pass
    return found


def scan(files: list[Path]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    secrets, personal = local_secrets(), personal_strings()

    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        if NEVER_SHIP.search(rel) and rel != ".env.example":
            errors.append(f"{rel}: этому файлу нельзя попадать в публикацию")
            continue
        if path.suffix.lower() in SKIP_SCAN_SUFFIXES or path.stat().st_size > TEXT_LIMIT:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        digits_text = re.sub(r"\D", "", text) if any(k.startswith("memory phone") for k in personal) else ""
        for label, value in secrets.items():
            if value in text:
                errors.append(f"{rel}: содержит ваш секрет из {label}")
        for label, value in personal.items():
            if label.startswith("memory phone"):
                if value in digits_text:
                    errors.append(f"{rel}: содержит ваш номер телефона ({label})")
            elif value.lower() in text.lower():
                errors.append(f"{rel}: содержит личные данные ({label} = «{value}»)")
        for label, pattern in KEY_PATTERNS.items():
            if pattern.search(text):
                errors.append(f"{rel}: похоже на {label}")
        m = LOCAL_PATH.search(text)
        if m:
            warnings.append(f"{rel}: путь с вашего компьютера «{m.group()}»")
    return errors, warnings


def check_catalog() -> list[str]:
    try:
        sys.path.insert(0, str(ROOT))
        import capabilities
        import tools

        registered = [getattr(t, "__name__", str(t)) for t in tools.FUNCTION_TOOLS]
        return [f"инструмент «{n}» не описан в capabilities.py" for n in capabilities.unlisted_tools(registered)]
    except Exception as exc:  # environment without deps installed
        return [f"не удалось проверить каталог возможностей: {exc}"]


def build_zip(files: list[Path], name: str) -> Path:
    out_dir = ROOT / "dist_release"
    out_dir.mkdir(exist_ok=True)
    target = out_dir / f"{name}-{time.strftime('%Y%m%d')}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, Path(name) / path.relative_to(ROOT))
    return target


def export_dir(files: list[Path], dest: Path) -> Path:
    """Unpacked clean copy: only the vetted file list, so no data/, .env, sessions or venv."""
    for path in files:
        target = dest / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--zip", action="store_true", help="если всё чисто — собрать чистый архив")
    parser.add_argument("--name", default="jarvis", help="имя папки внутри архива и префикс файла архива")
    parser.add_argument("--dir", type=Path, help="если всё чисто — ещё и выложить чистую копию в эту папку")
    parser.add_argument("--skip-catalog", action="store_true", help="не импортировать инструменты")
    args = parser.parse_args()

    files = published_files()
    print(f"Файлов к публикации: {len(files)}")
    errors, warnings = scan(files)
    if not args.skip_catalog:
        errors += check_catalog()
    if not (ROOT / "LICENSE").exists():
        warnings.append("нет файла LICENSE — без него код по умолчанию «все права защищены»; выберите лицензию осознанно")

    for w in warnings:
        print("  ! " + w)
    for e in errors:
        print("  ✗ " + e)
    if errors:
        print(f"\nНЕ ПУБЛИКУЙТЕ: найдено проблем — {len(errors)}.")
        return 1
    print("\nЧисто: секретов и личных данных в публикуемых файлах не найдено.")
    if args.zip:
        print("Архив:", build_zip(files, args.name))
    if args.dir:
        print("Чистая копия:", export_dir(files, args.dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
