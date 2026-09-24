"""Setup.exe: a self-contained Windows installer for Jarvis AI.

The finished installer is this program (frozen with PyInstaller, no console)
with a zip payload appended to the end of the file; it reads that payload
straight from its own .exe. Payload layout:

    manifest.json          {"version": ..., "name": ...}
    runtime/               embedded Python + every dependency (nothing is downloaded)
    app/                   Jarvis's own code
    Uninstall.exe, jarvis.ico
    redist/vc_redist.x64.exe   only run if the PC lacks the Visual C++ runtime

Per-user install (no administrator rights needed): %LOCALAPPDATA%\\Programs\\Jarvis AI.

Silent mode for scripts / IT:
    Setup.exe /S [/D=C:\\path] [/DESKTOP] [/AUTOSTART] [/LAUNCH]
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import zipfile
from pathlib import Path

import common
from common import APP_NAME, MARKER

LOG = Path(tempfile.gettempdir()) / "JarvisAI-setup.log"


def log(message: str) -> None:
    try:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {message}\n")
    except OSError:
        pass


def resource(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


class Options:
    def __init__(self) -> None:
        args = [a for a in sys.argv[1:]]
        low = [a.lower() for a in args]
        self.silent = "/s" in low or "/silent" in low
        self.desktop = "/desktop" in low
        self.autostart = "/autostart" in low
        self.launch = "/launch" in low
        self.install_dir: Path | None = None
        for a in args:
            if a.upper().startswith("/D="):
                self.install_dir = Path(a[3:].strip('"'))


def resolve_install_dir(chosen: Path) -> Path:
    """Never write into (or clean) a folder that already holds someone else's files."""
    chosen = chosen.expanduser()
    if chosen.exists() and any(chosen.iterdir()) and not (chosen / MARKER).exists():
        return chosen / APP_NAME
    return chosen


class Installer:
    """The actual work, UI-agnostic. `report(fraction, text)` drives whichever front end is active."""

    def __init__(self, install_dir: Path, desktop: bool, autostart: bool, report) -> None:
        self.dir = install_dir
        self.desktop = desktop
        self.autostart = autostart
        self.report = report
        self.zip = zipfile.ZipFile(common.exe_path())
        self.manifest = json.loads(self.zip.read("manifest.json"))

    @property
    def version(self) -> str:
        return self.manifest.get("version", "1.0")

    def total_bytes(self) -> int:
        return sum(i.file_size for i in self.zip.infolist() if not i.filename.startswith(("redist/", "manifest.json")))

    def run(self) -> None:
        log(f"install to {self.dir}")
        self.report(0.0, "Подготовка…")
        if (self.dir / MARKER).exists():
            self.report(0.0, "Закрываю запущенный Jarvis AI…")
            common.kill_processes_in(self.dir)
            time.sleep(1.0)
            self.report(0.0, "Удаляю старую версию (ваши данные сохраняются)…")
            common.rmtree_retry(self.dir / "runtime")
            if (self.dir / "app").exists():
                common.rmtree_retry(self.dir / "app", keep=common.KEEP_ON_UPGRADE)
        self.dir.mkdir(parents=True, exist_ok=True)

        self._ensure_vc_runtime()
        self._extract()
        (self.dir / MARKER).write_text(self.version, encoding="utf-8")

        self.report(0.97, "Создаю ярлыки…")
        self._shortcuts()
        common.write_uninstall_entry(self.dir, self.version, common.dir_size_bytes(self.dir) // 1024)
        self.report(1.0, "Готово")
        log("install finished")

    def _ensure_vc_runtime(self) -> None:
        if common.vc_runtime_present():
            return
        names = [n for n in self.zip.namelist() if n.startswith("redist/vc_redist")]
        if not names:
            log("VC runtime missing and no redistributable in payload")
            return
        self.report(0.0, "Устанавливаю компонент Microsoft Visual C++ (потребуется подтверждение Windows)…")
        temp = Path(tempfile.mkdtemp(prefix="jarvisai-vc-"))
        target = temp / "vc_redist.x64.exe"
        target.write_bytes(self.zip.read(names[0]))
        code = common.run_elevated(target, "/install /quiet /norestart")
        log(f"vc_redist exit {code}")
        shutil.rmtree(temp, ignore_errors=True)
        if code not in (0, 3010, 1638):
            raise RuntimeError(
                "Не удалось установить компонент Microsoft Visual C++ (он нужен для работы программы). "
                "Установите его вручную: https://aka.ms/vs/17/release/vc_redist.x64.exe — и запустите установку снова."
            )

    def _extract(self) -> None:
        entries = [i for i in self.zip.infolist() if not i.filename.startswith(("redist/", "manifest.json"))]
        total = max(1, sum(i.file_size for i in entries))
        done = 0
        last_report = 0.0
        for info in entries:
            dest = self.dir / info.filename
            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with self.zip.open(info) as src, dest.open("wb") as out:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    now = time.monotonic()
                    if now - last_report > 0.08:
                        last_report = now
                        self.report(0.95 * done / total, f"Копирую файлы… {done * 100 // total}%")

    def _shortcuts(self) -> None:
        pythonw = self.dir / "runtime" / "pythonw.exe"
        app_dir = self.dir / "app"
        icon = self.dir / "jarvis.ico"
        menu = common.start_menu_dir()
        common.create_shortcut(menu / f"{APP_NAME}.lnk", pythonw, f'"{app_dir / "app.py"}"', app_dir, icon,
                               "Голосовой помощник Jarvis AI")
        common.create_shortcut(menu / f"{APP_NAME} — панель управления.lnk", pythonw, f'"{app_dir / "panel.py"}"', app_dir, icon,
                               "Настройка ключей и обзор возможностей")
        common.create_shortcut(menu / f"Удалить {APP_NAME}.lnk", self.dir / "Uninstall.exe", "", self.dir, icon,
                               "Удаление Jarvis AI")
        if self.desktop:
            common.create_shortcut(common.desktop_shortcut(), pythonw, f'"{app_dir / "app.py"}"', app_dir, icon,
                                   "Голосовой помощник Jarvis AI")
        if self.autostart:
            common.create_shortcut(common.startup_shortcut(), pythonw, f'"{app_dir / "app.py"}"', app_dir, icon,
                                   "Jarvis AI — запуск вместе с Windows")

    def launch(self) -> None:
        pythonw = self.dir / "runtime" / "pythonw.exe"
        subprocess.Popen(
            [str(pythonw), str(self.dir / "app" / "app.py")], cwd=str(self.dir / "app"),
            creationflags=common.DETACHED_PROCESS | common.CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
        )


# ---------------------------------------------------------------------------
# Silent mode
# ---------------------------------------------------------------------------


def run_silent(opts: Options) -> int:
    target = resolve_install_dir(opts.install_dir or common.default_install_dir())
    try:
        inst = Installer(target, opts.desktop, opts.autostart, lambda f, t: None)
        inst.run()
        if opts.launch:
            inst.launch()
        return 0
    except Exception:
        log(traceback.format_exc())
        return 1


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------

NAVY, GOLD, INK, MUTED = "#0a121f", "#e9b44c", "#1b2433", "#5b6678"


def run_gui(opts: Options) -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    common.enable_dpi_awareness()
    root = tk.Tk()
    root.title(f"Установка {APP_NAME}")
    root.geometry("560x440")
    # Height only: SetProcessDpiAwareness(1) below stops Windows from
    # bitmap-stretching this window on a scaled display, but Tk doesn't scale
    # its own font/padding metrics to match the real DPI -- at >100% Windows
    # scaling (125%/150%, common on laptops) the page's content can genuinely
    # need more vertical room than 440px, pushing the footer's button below
    # the visible window with no way to reach it. _fit_window() below grows
    # the window automatically when that happens; this resizable() is only a
    # manual safety net in case a page still needs more room than computed.
    root.resizable(False, True)
    try:
        root.iconbitmap(str(resource("jarvis.ico")))
    except tk.TclError:
        pass
    style = ttk.Style(root)
    try:
        style.theme_use("vista")
    except tk.TclError:
        pass
    root.option_add("*Font", "{Segoe UI} 10")

    banner = tk.Canvas(root, width=560, height=84, bg=NAVY, highlightthickness=0)
    banner.pack(fill="x")
    banner.create_oval(26, 22, 66, 62, outline=GOLD, width=3)
    banner.create_oval(37, 33, 55, 51, fill=GOLD, outline=GOLD)
    banner.create_text(88, 42, text=APP_NAME, fill="white", font=("Segoe UI Semibold", 20), anchor="w")

    body = tk.Frame(root, padx=28, pady=20)
    body.pack(fill="both", expand=True)
    footer = tk.Frame(root, padx=28, pady=14)
    footer.pack(fill="x")

    try:
        probe = Installer(Path("."), False, False, lambda f, t: None)
        need_mb = probe.total_bytes() // (1024 * 1024)
        version = probe.version
    except Exception as exc:
        messagebox.showerror(APP_NAME, f"Установщик повреждён: не найден пакет с файлами.\n\n{exc}")
        return 1

    dir_var = tk.StringVar(value=str(opts.install_dir or common.default_install_dir()))
    desktop_var = tk.BooleanVar(value=True)
    autostart_var = tk.BooleanVar(value=False)
    launch_var = tk.BooleanVar(value=True)
    events: queue.Queue = queue.Queue()
    state = {"result": None, "dir": None}

    def clear() -> None:
        for frame in (body, footer):
            for child in frame.winfo_children():
                child.destroy()

    def label(parent, text, bold=False, color=INK, wrap=470, **grid):
        w = tk.Label(parent, text=text, fg=color, justify="left", anchor="w", wraplength=wrap,
                     font=("Segoe UI Semibold", 10) if bold else ("Segoe UI", 10))
        w.pack(anchor="w", **grid)
        return w

    def button(text, command, primary=False, side="right"):
        b = ttk.Button(footer, text=text, command=command, width=16)
        b.pack(side=side, padx=(8, 0))
        return b

    def fit_window() -> None:
        """Grows the window (never shrinks) to fit whatever the current page
        just laid out -- see the resizable() comment above for why this is
        needed at all. Call at the end of every page_*() function, after its
        widgets are packed."""
        root.update_idletasks()
        need_h = root.winfo_reqheight()
        if need_h > root.winfo_height():
            root.geometry(f"{root.winfo_width()}x{need_h}")

    # -- page 1: options --------------------------------------------------
    def page_options() -> None:
        clear()
        label(body, f"Добро пожаловать в установку {APP_NAME}", bold=True, pady=(0, 6))
        label(body, "Голосовой помощник для Windows: слушает, отвечает голосом и делает дела на компьютере. "
                    "Всё нужное уже внутри — Python и дополнительные компоненты ставить не придётся.", color=MUTED, pady=(0, 16))
        label(body, "Папка установки", bold=True, pady=(0, 4))
        row = tk.Frame(body)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=dir_var).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Обзор…", width=10,
                   command=lambda: dir_var.set(filedialog.askdirectory(initialdir=dir_var.get(), title="Папка установки") or dir_var.get())
                   ).pack(side="left", padx=(8, 0))
        label(body, f"Понадобится около {need_mb} МБ. Права администратора не нужны.", color=MUTED, pady=(6, 14))
        ttk.Checkbutton(body, text="Ярлык на рабочем столе", variable=desktop_var).pack(anchor="w", pady=2)
        ttk.Checkbutton(body, text="Запускать вместе с Windows", variable=autostart_var).pack(anchor="w", pady=2)
        button("Отмена", root.destroy, side="right")
        button("Установить", start_install, primary=True, side="right")
        fit_window()

    # -- page 2: progress -------------------------------------------------
    progress = {"bar": None, "text": None}

    def start_install() -> None:
        chosen = Path(dir_var.get().strip().strip('"'))
        if not chosen.is_absolute():
            messagebox.showwarning(APP_NAME, "Укажите полный путь к папке, например C:\\Programs\\Jarvis AI.")
            return
        target = resolve_install_dir(chosen)
        anchor = target
        while not anchor.exists() and anchor.parent != anchor:
            anchor = anchor.parent
        try:
            free_mb = shutil.disk_usage(anchor).free // (1024 * 1024)
        except OSError:
            free_mb = need_mb * 10
        if free_mb < need_mb * 1.2:
            messagebox.showwarning(APP_NAME, f"Не хватает места на диске: нужно около {need_mb} МБ, свободно {free_mb} МБ.")
            return
        state["dir"] = target
        clear()
        label(body, "Устанавливаю…", bold=True, pady=(0, 12))
        progress["text"] = label(body, "Подготовка…", color=MUTED, pady=(0, 10))
        progress["bar"] = ttk.Progressbar(body, length=500, maximum=1000)
        progress["bar"].pack(anchor="w")
        label(body, "Это займёт пару минут. Окно закрывать не нужно.", color=MUTED, pady=(14, 0))
        fit_window()

        def work() -> None:
            try:
                Installer(target, desktop_var.get(), autostart_var.get(), lambda f, t: events.put(("p", f, t))).run()
                events.put(("done", None, None))
            except Exception as exc:
                log(traceback.format_exc())
                events.put(("error", str(exc), None))

        threading.Thread(target=work, daemon=True).start()
        root.after(60, poll)

    def poll() -> None:
        finished = False
        try:
            while True:
                kind, a, b = events.get_nowait()
                if kind == "p":
                    progress["bar"]["value"] = int(a * 1000)
                    progress["text"].config(text=b)
                elif kind == "done":
                    finished = True
                    page_done()
                elif kind == "error":
                    finished = True
                    page_error(a)
        except queue.Empty:
            pass
        if not finished:
            root.after(60, poll)

    # -- page 3: result ---------------------------------------------------
    def page_done() -> None:
        clear()
        label(body, f"{APP_NAME} установлен", bold=True, pady=(0, 8))
        label(body, "При первом запуске откроется панель управления: там нужно один раз ввести ключи Claude и Deepgram "
                    "(подробнее — на первой странице панели). Дальше Джарвис работает из значка в трее.", color=MUTED, pady=(0, 16))
        ttk.Checkbutton(body, text="Запустить Jarvis AI и открыть панель настройки", variable=launch_var).pack(anchor="w")

        def finish() -> None:
            if launch_var.get():
                try:
                    Installer(state["dir"], False, False, lambda f, t: None).launch()
                except Exception:
                    log(traceback.format_exc())
            root.destroy()

        button("Готово", finish, primary=True)
        fit_window()

    def page_error(message: str) -> None:
        clear()
        label(body, "Установка не завершилась", bold=True, color="#b3261e", pady=(0, 8))
        label(body, message, pady=(0, 10))
        label(body, f"Подробности записаны в {LOG}", color=MUTED)
        button("Закрыть", root.destroy, primary=True)
        button("Повторить", page_options, side="right")
        fit_window()

    page_options()
    root.mainloop()
    return 0


def main() -> int:
    opts = Options()
    if opts.silent:
        return run_silent(opts)
    return run_gui(opts)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log(traceback.format_exc())
        raise
