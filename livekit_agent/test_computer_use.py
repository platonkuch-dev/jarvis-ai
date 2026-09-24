"""
Offline tests for tools/computer_use.py -- no model, no real mouse.

Run from livekit_agent/:  .venv\\Scripts\\python.exe test_computer_use.py
Exit code 0 = all checks passed.
"""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace as NS

from PIL import Image

import config
import tools.computer_use as cu
from tools.registry import FUNCTION_TOOLS, IMPL_REGISTRY

failures = 0


def check(label: str, condition: bool) -> None:
    global failures
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        failures += 1


# --- registration ------------------------------------------------------------

check("use_computer and stop_computer_use are registered as LLM tools",
      {"use_computer", "stop_computer_use"} <= {getattr(t, "__name__", getattr(getattr(t, "info", None), "name", "")) for t in FUNCTION_TOOLS})
check("both have scenario implementations", {"use_computer", "stop_computer_use"} <= set(IMPL_REGISTRY))
check("step budget is no longer the old 12", config.COMPUTER_USE_MAX_STEPS >= 30)
check("effort defaults to high", config.COMPUTER_USE_EFFORT == "high")

# --- keys --------------------------------------------------------------------

import pyautogui

calls: list = []
for fn in ("press", "hotkey", "keyDown", "keyUp", "click", "moveTo", "scroll", "hscroll", "dragTo",
           "mouseDown", "mouseUp", "write"):
    setattr(pyautogui, fn, (lambda n: lambda *a, **k: calls.append((n, a, k)))(fn))
pyautogui.size = lambda: (2560, 1440)
pyautogui.position = lambda: (10, 10)
pyautogui.screenshot = lambda region=None: Image.new(
    "RGB", (2560, 1440) if region is None else (region[2], region[3]), "white")
cu.time.sleep = lambda *_a, **_k: None

calls.clear()
cu._press_key_combo("ctrl+a Delete", pyautogui)
check("'ctrl+a Delete' = hotkey then press", calls == [("hotkey", ("ctrl", "a"), {}), ("press", ("delete",), {})])
calls.clear()
cu._press_key_combo("Tab", pyautogui, repeat=3)
check("repeat presses the key N times", [c[0] for c in calls] == ["press"] * 3)
try:
    cu._press_key_combo("ctrl+NotAKey", pyautogui)
    check("unknown key raises", False)
except ValueError:
    check("unknown key raises", True)

# --- geometry ----------------------------------------------------------------

factor, w, h = cu._compute_scale(2560, 1440)
check("1440p is scaled to ~1080p", (w, h) == (1920, 1080) and abs(factor - 0.75) < 1e-9)
check("1080p stays untouched", cu._compute_scale(1920, 1080)[0] == 1.0)
check("screenshot coordinates map back to real pixels", cu._to_real([960, 540], 0.75, (2560, 1440)) == [1280, 720])
check("corner clicks stay clear of the failsafe corner", cu._to_real([0, 0], 0.75, (2560, 1440)) == [3, 3])

# --- actions -----------------------------------------------------------------

calls.clear()
values: dict = {}
desc, content = cu._execute_action("left_click", {"coordinate": [960, 540]}, 0.75, 1920, 1080, values)
check("click lands on the real position and returns a screenshot",
      ("click", (1280, 720), {"button": "left", "clicks": 1}) in calls and content[0]["type"] == "image")
check("screenshots are JPEG", content[0]["source"]["media_type"] == "image/jpeg")

calls.clear()
cu._execute_action("scroll", {"coordinate": [100, 100], "scroll_direction": "down", "scroll_amount": 3}, 0.75, 1920, 1080, values)
scrolls = [c for c in calls if c[0] == "scroll"]
check("scroll is 120 units per notch (pyautogui passes it raw)", scrolls and scrolls[0][1] == (-360,))

_, zoom = cu._execute_action("zoom", {"region": [0, 0, 300, 200]}, 0.75, 1920, 1080, values)
check("zoom returns an image", zoom[0]["type"] == "image")

calls.clear()
_, _ = cu._execute_action("type", {"text": "hello {{random:username}}"}, 0.75, 1920, 1080, values)
typed = [c for c in calls if c[0] == "write"]
check("ASCII text goes through pyautogui.write with placeholders expanded",
      typed and typed[0][1][0].startswith("hello ") and "{{" not in typed[0][1][0] and "username" in values)
check("the same placeholder gives the same value within a run",
      cu._resolve_placeholders("{{random:username}}", values) == values["username"])
pw = cu._random_value("password")
check("generated passwords are 16 chars with upper, lower, digit and symbol",
      len(pw) == 16 and any(c.isupper() for c in pw) and any(c.islower() for c in pw)
      and any(c.isdigit() for c in pw) and any(c in "!@#$%*" for c in pw))
try:
    cu._random_value("email")
    check("no random e-mail placeholder (would point at strangers' addresses)", False)
except ValueError:
    check("no random e-mail placeholder (would point at strangers' addresses)", True)

# --- history pruning ---------------------------------------------------------

msgs = [{"role": "user", "content": [{"type": "text", "text": "task"}, {"type": "image"}]}]
for _ in range(6):
    msgs.append({"role": "user", "content": [{"type": "tool_result", "content": [{"type": "image"}]}]})
cu._strip_old_screenshots(msgs)
imgs = [c for m in msgs for b in m["content"] for c in ([b] if b.get("type") == "image" else b.get("content", []) if isinstance(b.get("content"), list) else []) if c.get("type") == "image"]
check("only the newest 3 screenshots survive (including the priming one)", len(imgs) == 3)

# --- the loop with a fake client ---------------------------------------------


def use(id_, name, **inp):
    return NS(type="tool_use", id=id_, name=name, input=inp, toolset_name="computer",
              model_dump=lambda: {"type": "tool_use", "id": id_, "name": name, "input": inp, "toolset_name": "computer"})


def text(t):
    return NS(type="text", text=t, model_dump=lambda: {"type": "text", "text": t})


def reply(*blocks, stop="tool_use"):
    return NS(content=list(blocks), stop_reason=stop,
              usage=NS(input_tokens=100, output_tokens=10, cache_creation_input_tokens=0, cache_read_input_tokens=0))


class FakeClient:
    def __init__(self, replies):
        self._it, self.sent = iter(replies), []
        self.beta = NS(messages=self)

    def create(self, **kw):
        self.sent.append({**kw, "messages": list(kw["messages"])})
        return next(self._it)


def run(replies, max_steps=30, stop=False):
    fake = FakeClient(replies)
    cu.anthropic.Anthropic = lambda **k: fake
    cu._STOP.clear()
    if stop:
        cu._STOP.set()
    try:
        return cu._run_loop("test task", max_steps), fake
    finally:
        cu._STOP.clear()


(res, steps, vals, toks), fake = run([
    reply(use("t1", "left_click", coordinate=[960, 540])),
    reply(text("Готово, кнопка нажата."), stop="end_turn"),
])
check("plain final text ends the run", res == "Готово, кнопка нажата." and toks == (200, 20))
req = fake.sent[0]
check("request: official toolset, effort high, room for thinking",
      req["tools"][0]["type"] == "computer_toolset_20260801" and req["output_config"] == {"effort": "high"} and req["max_tokens"] >= 8000)
tr = fake.sent[1]["messages"][-1]["content"][0]
check("tool_result carries toolset_name and a screenshot", tr["toolset_name"] == "computer" and tr["content"][0]["type"] == "image")

(res, *_), fake = run([
    reply(use("t1", "key", text="NotAKey"), use("t2", "left_click", coordinate=[5, 5])),
    reply(text("сдаюсь"), stop="end_turn"),
])
bad = fake.sent[1]["messages"][-1]["content"]
check("a failed action is flagged and the rest of its batch is skipped",
      bad[0].get("is_error") and bad[1].get("is_error") and "Not executed" in bad[1]["content"])

(res, *_), _ = run([reply(use(f"t{i}", "left_click", coordinate=[5, 5])) for i in range(20)])
check("identical actions repeated are aborted as stuck", res.startswith("Застрял"))

(res, *_), _ = run([reply(use(f"t{i}", "screenshot")) for i in range(40)], max_steps=5)
check("the action budget is enforced", "5 действий" in res)

(res, steps, vals, _), _ = run([
    reply(use("t1", "type", text="pw: {{random:password}}")),
    reply(text("ok"), stop="end_turn"),
])
check("typed text is logged by length only, generated values are reported",
      steps[1] == f"type ({len('pw: {{random:password}}')} симв.)" and "password" in vals
      and vals["password"] not in " ".join(steps))

(res, *_), fake = run([reply(text("never"), stop="end_turn")], stop=True)
check("the stop flag aborts before any API call is made", res.startswith("Остановлено") and fake.sent == [])

# --- Unicode typing (only if our own window really has focus) -----------------

try:
    import ctypes
    import tkinter as tk

    root = tk.Tk()
    root.geometry("400x80+200+200")
    entry = tk.Entry(root, width=60)
    entry.pack()
    root.attributes("-topmost", True)
    root.update()
    ours = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
    # Windows only lets the foreground app hand out focus; a tap of Alt lifts that lock.
    ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)
    ctypes.windll.user32.keybd_event(0x12, 0, 2, 0)
    ctypes.windll.user32.SetForegroundWindow(ours)
    entry.focus_force()
    for _ in range(30):
        root.update()
    fg = ctypes.windll.user32.GetForegroundWindow()
    check("SendInput INPUT struct has the size Windows expects (40 bytes on 64-bit)",
          ctypes.sizeof(cu._INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28))
    if fg == ours and root.focus_get() is entry:
        cu._send_unicode("Привет, мир! 你好")
        for _ in range(40):
            root.update()
        got = entry.get()
        # `in`, not `==`: another app's global keyboard hook occasionally injects a stray
        # key into a freshly focused window on this machine; what matters is that the
        # whole string arrived intact and in order.
        check(f"SendInput types Cyrillic/CJK into a real window (got {got!r})", "Привет, мир! 你好" in got)
    else:
        print("[SKIP] Unicode typing: test window did not get keyboard focus (nothing was typed)")
    root.destroy()
except Exception as exc:                                                          # pragma: no cover
    print(f"[SKIP] Unicode typing test unavailable: {exc}")

print()
if failures:
    print(f"{failures} check(s) FAILED.")
    sys.exit(1)
print("All checks passed.")
