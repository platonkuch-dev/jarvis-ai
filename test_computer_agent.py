"""
Offline tests for actions/computer_agent.py -- no model, no real mouse.

The Claude client and pyautogui's input functions are replaced with fakes, so
this pins down the parts that are easy to get subtly wrong: key handling,
placeholders, screenshot scaling and pruning, and the loop's stop conditions
(done / needs_user / failed / stuck / action limit / stop request).

Run directly:  python test_computer_agent.py
Exit code 0 = all checks passed.
"""
from __future__ import annotations

import json
import sys

import actions.computer_agent as ca

failures = 0


def check(label: str, condition: bool) -> None:
    global failures
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        failures += 1


# --- keys --------------------------------------------------------------------

check("hotkey string is split and normalised", ca._split_keys("Control + L") == ["ctrl", "l"])
check("Return/Escape aliases", ca._norm_key("Return") == "enter" and ca._norm_key("Escape") == "esc")

# --- placeholders ------------------------------------------------------------

vals: dict = {}
first = ca._resolve_placeholders("{{random:email}}", vals)
second = ca._resolve_placeholders("again {{random:email}}", vals)
check("random placeholder is stable within a run", second == f"again {first}" and "@" in first)
check("text without placeholders is untouched", ca._resolve_placeholders("hello", {}) == "hello")

# --- Claude's official computer-use tool -------------------------------------

from types import SimpleNamespace as NS
from PIL import Image

ca.time.sleep = lambda *_a, **_k: None

check("scale: 1440p screen is shrunk to 1080p", abs(ca._native_scale(2560, 1440) - 0.75) < 1e-9)
check("scale: 1080p screen is left alone", ca._native_scale(1920, 1080) == 1.0)
check("scale: small screen is never upscaled", ca._native_scale(1280, 720) == 1.0)

nctx = {"scale": 0.75, "screen": (2560, 1440), "values": {}}
check("screenshot coordinates map back to real pixels", ca._native_point([960, 540], nctx) == (1280, 720))
check("corner clicks stay clear of the failsafe corner", ca._native_point([0, 0], nctx) == (3, 3))

stub = [[{"type": "image"}, {"type": "text", "text": "x"}] for _ in range(15)]
msgs = [{"role": "user", "content": [{"type": "tool_result", "content": c}]} for c in stub]
ca._prune_screenshots(msgs)
left = sum(1 for c in stub for it in c if it["type"] == "image")
check("old screenshots are pruned down to the newest 3", left == 3 and stub[-1][0]["type"] == "image" and stub[0][0]["type"] == "text")
few = [[{"type": "image"}] for _ in range(6)]
ca._prune_screenshots([{"role": "user", "content": [{"type": "tool_result", "content": c}]} for c in few])
check("pruning waits until there is enough slack (keeps the cache prefix stable)", all(c[0]["type"] == "image" for c in few))

calls: list = []
ca.pyautogui.size = lambda: (2560, 1440)
ca.pyautogui.position = lambda: (10, 10)
ca.pyautogui.screenshot = lambda region=None: Image.new("RGB", (2560, 1440) if region is None else (region[2], region[3]), "white")
for fn in ("moveTo", "click", "press", "hotkey", "keyDown", "keyUp", "scroll", "hscroll", "dragTo", "mouseDown", "mouseUp"):
    setattr(ca.pyautogui, fn, (lambda n: lambda *a, **k: calls.append((n, a, k)))(fn))
ca.pyautogui.typewrite = lambda *a, **k: calls.append(("typewrite", a, k))
ca._PYPERCLIP = False          # never touch the real clipboard in tests


def use(id_, name, **inp):
    return NS(type="tool_use", id=id_, name=name, input=inp,
              toolset_name="computer" if name in ca._COMPUTER_MEMBERS else None)


def reply(*blocks, stop="tool_use"):
    return NS(content=list(blocks), stop_reason=stop,
              usage=NS(input_tokens=100, output_tokens=10, cache_creation_input_tokens=0, cache_read_input_tokens=0))


class FakeClient:
    def __init__(self, replies):
        self._it, self.sent = iter(replies), []
        self.messages = self

    def create(self, **kw):
        self.sent.append({**kw, "messages": list(kw["messages"])})   # the loop keeps appending to its own list
        return next(self._it)


def run_claude(replies, **kw):
    fake = FakeClient(replies)
    ca._build_anthropic_client = lambda **k: fake
    return ca._run_claude(kw.get("goal", "test"), kw.get("max_steps", 10), 60, None), fake


calls.clear()
res, fake = run_claude([
    reply(use("t1", "screenshot")),
    reply(use("t2", "left_click", coordinate=[960, 540]), use("t3", "key", text="ctrl+a Delete")),
    reply(use("t4", "agent_status", status="done", message="all good")),
])
check("Claude loop ends with Done and reports token totals", res.startswith("Done: all good") and "tokens in/out: 300/30" in res)
check("click landed on the real screen position", ("click", (1280, 720), {"button": "left", "clicks": 1}) in calls)
check("'ctrl+a Delete' becomes a hotkey then a key press", ("hotkey", ("ctrl", "a"), {}) in calls and ("press", ("delete",), {}) in calls)
first_results = fake.sent[1]["messages"][-1]["content"]
check("screenshot result is an image block tagged with the computer toolset",
      first_results[0].get("toolset_name") == "computer" and first_results[0]["content"][0]["type"] == "image")
check("request uses the official toolset with no legacy display params",
      fake.sent[0]["tools"][0] == {"type": "computer_toolset_20260801"} and "display_width_px" not in json.dumps(fake.sent[0]["tools"]))
check("request asks for prompt caching and effort", fake.sent[0]["cache_control"] == {"type": "ephemeral"} and fake.sent[0]["output_config"]["effort"] == "high")
check("PowerShell tool is offered by default", any(t.get("name") == "run_powershell" for t in fake.sent[0]["tools"]))

ca.os.environ["JARVIS_AGENT_SHELL"] = "off"
_, fake = run_claude([reply(use("t1", "agent_status", status="done", message="x"))])
check("JARVIS_AGENT_SHELL=off removes the PowerShell tool", all(t.get("name") != "run_powershell" for t in fake.sent[0]["tools"]))
del ca.os.environ["JARVIS_AGENT_SHELL"]

res, _ = run_claude([reply(use("t1", "agent_status", status="needs_user", message="solve the captcha"))])
check("needs_user is reported as [NEEDS_USER]", res.startswith("[NEEDS_USER] solve the captcha"))

res, _ = run_claude([reply(use("t1", "agent_status", status="failed", message="site is down"))])
check("failed is reported as Failed", res.startswith("Failed: site is down"))

res, fake = run_claude([
    reply(use("t1", "key", text="NotARealKey"), use("t2", "left_click", coordinate=[1, 1])),
    reply(use("t3", "agent_status", status="failed", message="gave up")),
])
bad = fake.sent[1]["messages"][-1]["content"]
check("a failed action is flagged and the rest of the batch is not executed",
      bad[0].get("is_error") and bad[1].get("is_error") and "Not executed" in bad[1]["content"])

res, _ = run_claude([reply(NS(type="text", text="Nothing more to do."), stop="end_turn")])
check("a plain final text reply counts as done", res.startswith("Done: Nothing more to do."))

res, _ = run_claude([reply(use(f"t{i}", "left_click", coordinate=[5, 5])) for i in range(20)])
check("repeating the same action is aborted as stuck", "Stuck" in res)

res, _ = run_claude([reply(use(f"t{i}", "screenshot")) for i in range(30)], max_steps=3)
check("the action budget is enforced", "action limit" in res)

res, fake = run_claude([
    reply(use("t1", "type", text="{{random:email}}")),
    reply(use("t2", "agent_status", status="done", message="ok")),
])
check("placeholders in typed text are expanded and the values saved",
      any(c[0] == "typewrite" and "@" in c[1][0] for c in calls) and "generated_values.json" in res)

print()
if failures:
    print(f"{failures} check(s) FAILED.")
    sys.exit(1)
print("All checks passed.")
