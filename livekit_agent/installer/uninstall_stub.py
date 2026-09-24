"""Uninstall.exe: removes Jarvis AI (registered in Settings > Apps).

A program can't delete its own running .exe, so stage 1 copies itself to the
temp folder and starts that copy (stage 2) against the install folder.

    Uninstall.exe            asks first, keeps your data unless you tick the box
    Uninstall.exe /S         silent, keeps data
    Uninstall.exe /S /PURGE  silent, removes data too (memory, keys, logs)

"Data" is app\\data (memory, notes, Telegram session, voices), app\\.env (API
keys) and app\\logs. By default they stay, so a reinstall picks up where you left off.
"""

from __future__ import annotations

import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import common
from common import APP_NAME, MARKER

EXTENSION_ID = "com.jarvis.adobebridge.panel"  # what the app installs into Adobe's CEP folder
LOG = Path(tempfile.gettempdir()) / "JarvisAI-uninstall.log"


def log(message: str) -> None:
    try:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {message}\n")
    except OSError:
        pass


def uninstall(install_dir: Path, purge: bool, report) -> None:
    report(0.05, "Закрываю Jarvis AI…")
    common.kill_processes_in(install_dir)
    time.sleep(1.0)

    report(0.25, "Удаляю ярлыки…")
    shutil.rmtree(common.start_menu_dir(), ignore_errors=True)
    for lnk in (common.desktop_shortcut(), common.startup_shortcut()):
        try:
            lnk.unlink()
        except OSError:
            pass

    appdata = os.environ.get("APPDATA")
    if appdata:
        shutil.rmtree(Path(appdata) / "Adobe" / "CEP" / "extensions" / EXTENSION_ID, ignore_errors=True)

    report(0.45, "Удаляю программу…")
    # Kept data leaves the folder marked as ours, so a later reinstall goes into the same place.
    common.rmtree_retry(install_dir, keep=set() if purge else {"app", MARKER})
    if not purge and (install_dir / "app").exists():
        # keep only the user's data inside app\
        common.rmtree_retry(install_dir / "app", keep=common.KEEP_ON_UPGRADE)
    common.delete_uninstall_entry()

    report(0.95, "Завершаю…")
    try:
        install_dir.rmdir()  # only succeeds if empty, i.e. nothing of the user's was kept
    except OSError:
        pass
    report(1.0, "Готово")


def stage1(args: list[str]) -> int:
    """Copy ourselves out of the install folder, then continue from the copy."""
    import subprocess

    install_dir = common.read_install_location() or common.exe_path().parent
    copy = Path(tempfile.gettempdir()) / f"JarvisAI-uninstall-{os.getpid()}.exe"
    shutil.copyfile(common.exe_path(), copy)
    subprocess.Popen(
        [str(copy), "--stage2", str(install_dir), *args], creationflags=common.DETACHED_PROCESS | common.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
    )
    return 0


def schedule_self_delete() -> None:
    import subprocess

    me = common.exe_path()
    # A single string, not a list: subprocess would escape the inner quotes as \" which cmd.exe doesn't understand.
    subprocess.Popen(
        f'cmd.exe /d /c ping -n 5 127.0.0.1 >nul & del /f /q "{me}"',
        creationflags=common.CREATE_NO_WINDOW, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def stage2(install_dir: Path, silent: bool, purge: bool) -> int:
    if not (install_dir / MARKER).exists() and not (install_dir / "runtime").exists():
        # Not one of our folders (e.g. registry points somewhere stale): only clean the registration.
        common.delete_uninstall_entry()
        return 0
    if silent:
        try:
            uninstall(install_dir, purge, lambda f, t: None)
            return 0
        except Exception:
            log(traceback.format_exc())
            return 1
        finally:
            schedule_self_delete()

    import tkinter as tk
    from tkinter import messagebox, ttk

    common.enable_dpi_awareness()
    root = tk.Tk()
    root.title(f"Удаление {APP_NAME}")
    root.geometry("500x290")
    root.resizable(False, False)
    try:
        ttk.Style(root).theme_use("vista")
    except tk.TclError:
        pass
    root.option_add("*Font", "{Segoe UI} 10")
    body = tk.Frame(root, padx=28, pady=22)
    body.pack(fill="both", expand=True)
    purge_var = tk.BooleanVar(value=False)
    events: queue.Queue = queue.Queue()

    def label(text, bold=False, color="#1b2433", **pack):
        tk.Label(body, text=text, justify="left", anchor="w", wraplength=440, fg=color,
                 font=("Segoe UI Semibold", 11) if bold else ("Segoe UI", 10)).pack(anchor="w", **pack)

    def confirm_page() -> None:
        for child in body.winfo_children():
            child.destroy()
        label(f"Удалить {APP_NAME} с этого компьютера?", bold=True, pady=(0, 8))
        label("Программа будет закрыта и удалена. По умолчанию ваши данные остаются на диске: память ассистента, "
              "ключи API, вход в Telegram, журналы — при повторной установке всё вернётся.", color="#5b6678", pady=(0, 12))
        ttk.Checkbutton(body, text="Удалить также мои данные и ключи API", variable=purge_var).pack(anchor="w", pady=(0, 18))
        row = tk.Frame(body)
        row.pack(anchor="e", fill="x")
        ttk.Button(row, text="Удалить", width=14, command=start).pack(side="right", padx=(8, 0))
        ttk.Button(row, text="Отмена", width=14, command=lambda: (root.destroy(), schedule_self_delete())).pack(side="right")

    bar = {}

    def start() -> None:
        for child in body.winfo_children():
            child.destroy()
        label("Удаляю…", bold=True, pady=(0, 12))
        bar["text"] = tk.Label(body, text="", anchor="w", fg="#5b6678")
        bar["text"].pack(anchor="w", pady=(0, 8))
        bar["bar"] = ttk.Progressbar(body, length=440, maximum=1000)
        bar["bar"].pack(anchor="w")

        def work() -> None:
            try:
                uninstall(install_dir, purge_var.get(), lambda f, t: events.put(("p", f, t)))
                events.put(("done", None, None))
            except Exception as exc:
                log(traceback.format_exc())
                events.put(("error", str(exc), None))

        threading.Thread(target=work, daemon=True).start()
        root.after(60, poll)

    def poll() -> None:
        try:
            while True:
                kind, a, b = events.get_nowait()
                if kind == "p":
                    bar["bar"]["value"] = int(a * 1000)
                    bar["text"].config(text=b)
                elif kind == "done":
                    messagebox.showinfo(APP_NAME, f"{APP_NAME} удалён.")
                    root.destroy()
                    return
                elif kind == "error":
                    messagebox.showerror(APP_NAME, f"Не всё удалось удалить:\n\n{a}\n\nПодробности: {LOG}")
                    root.destroy()
                    return
        except queue.Empty:
            pass
        root.after(60, poll)

    confirm_page()
    root.mainloop()
    schedule_self_delete()
    return 0


def main() -> int:
    args = sys.argv[1:]
    low = [a.lower() for a in args]
    if "--stage2" in low:
        idx = low.index("--stage2")
        install_dir = Path(args[idx + 1])
        return stage2(install_dir, silent="/s" in low, purge="/purge" in low)
    return stage1([a for a in args])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log(traceback.format_exc())
        raise
