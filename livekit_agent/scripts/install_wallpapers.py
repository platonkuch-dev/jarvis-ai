"""Install Jarvis as Wallpaper Engine web wallpapers and put them on the monitors.

    python scripts/install_wallpapers.py                 # face -> main monitor, plan -> second
    python scripts/install_wallpapers.py --face 0 --plan 1   # Wallpaper Engine's own monitor numbers
    python scripts/install_wallpapers.py --restore       # put the previous wallpapers back

Two projects land in <Wallpaper Engine>/projects/myprojects (they also show up in its
"Installed" list): "Jarvis — лицо" and "Jarvis — план дня". Each is a tiny wrapper page
(wallpaper/wrapper.html) that shows the live page from Jarvis's panel server on localhost.
The wallpapers that were set before are saved to data/wallpaper_backup.json for --restore.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402

BACKUP = config.DATA_DIR / "wallpaper_backup.json"
PROJECTS = {
    "jarvis_face": ("Jarvis — лицо", "/?mode=face", "Голографический Джарвис: лицо, статус, субтитры, микрофон."),
    "jarvis_plan": ("Jarvis — план дня", "/plan.html?wallpaper=1", "План дня Джарвиса: кольцо суток, события, дела, таймеры."),
}


def find_engine() -> Path | None:
    try:
        import psutil

        for proc in psutil.process_iter(["name", "exe"]):
            if (proc.info.get("name") or "").lower() in ("wallpaper64.exe", "wallpaper32.exe") and proc.info.get("exe"):
                return Path(proc.info["exe"]).parent
    except Exception:
        pass
    for guess in (Path("C:/steam/steamapps/common/wallpaper_engine"),
                  Path("C:/Program Files (x86)/Steam/steamapps/common/wallpaper_engine")):
        if (guess / "wallpaper64.exe").exists():
            return guess
    return None


def current_wallpapers(engine: Path) -> dict:
    try:
        cfg = json.loads((engine / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    for value in cfg.values():
        if isinstance(value, dict) and "general" in value:
            return value["general"].get("wallpaperconfig", {}).get("selectedwallpapers", {})
    return {}


def open_wallpaper(engine: Path, file: str, monitor: int) -> None:
    subprocess.run([str(engine / "wallpaper64.exe"), "-control", "openWallpaper", "-file", file,
                    "-monitor", str(monitor)], check=False, timeout=30)


def install(engine: Path) -> dict[str, Path]:
    template = (ROOT / "wallpaper" / "wrapper.html").read_text(encoding="utf-8")
    out = {}
    for folder, (title, path, desc) in PROJECTS.items():
        dest = engine / "projects" / "myprojects" / folder
        dest.mkdir(parents=True, exist_ok=True)
        html = (template.replace("__TITLE__", title)
                .replace("__URL__", f"http://127.0.0.1:{config.HUD_PANEL_PORT}{path}")
                .replace("__PORT__", str(config.HUD_PANEL_PORT)))
        (dest / "index.html").write_text(html, encoding="utf-8")
        project = {"file": "index.html", "title": title, "description": desc, "type": "web",
                   "general": {"properties": {}}}
        preview = ROOT / "wallpaper" / f"{folder}.jpg"
        if preview.exists():
            shutil.copy2(preview, dest / "preview.jpg")
            project["preview"] = "preview.jpg"
        (dest / "project.json").write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
        out[folder] = dest / "project.json"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--face", type=int, default=0, help="Wallpaper Engine monitor number for the face")
    ap.add_argument("--plan", type=int, default=1, help="Wallpaper Engine monitor number for the plan")
    ap.add_argument("--restore", action="store_true")
    args = ap.parse_args()

    engine = find_engine()
    if engine is None:
        print("Wallpaper Engine не найден. Запустите его и повторите.")
        return 1

    if args.restore:
        try:
            saved = json.loads(BACKUP.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print("Нет сохранённых прежних обоев.")
            return 1
        for key, entry in saved.items():
            if key.lower().startswith("monitor") and entry.get("file"):
                open_wallpaper(engine, entry["file"], int(key[len("monitor"):]))
        print("Прежние обои возвращены.")
        return 0

    before = current_wallpapers(engine)
    if before and not any("jarvis_" in str(v.get("file", "")) for v in before.values()):
        BACKUP.write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8")
    files = install(engine)
    open_wallpaper(engine, str(files["jarvis_face"]), args.face)
    open_wallpaper(engine, str(files["jarvis_plan"]), args.plan)
    print(f"Готово: лицо -> монитор {args.face}, план -> монитор {args.plan} (нумерация Wallpaper Engine).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
