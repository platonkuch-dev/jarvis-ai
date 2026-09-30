"""
actions/self_extend.py -- the `add_capability` tool: JARVIS writing new
capabilities for itself, using Claude, on request.

Classified DANGEROUS in core/tool_registry.py, but per the user's explicit
request (2026-09-03) it is no longer force-gated in
core/tool_dispatch.py's `_LOCALLY_GATED_TOOLS` -- this handler now runs
immediately on the first call, with no confirmation step. The gating
mechanism itself is still there (core/tool_dispatch.py, core/custom_tools
.py's register_tool()) for a self-authored tool whose own generated risk
assessment calls for staying gated after creation -- that's Claude's
per-tool judgment at generation time, a separate decision from whether
*creating* a new tool needs confirming, which it no longer does.

Reuses actions/dev_agent.py's Claude-calling plumbing (`_generate`,
`_strip_fences`) rather than duplicating it -- this is the same
"Claude writes code" pattern, just producing one permanent tool module
instead of a throwaway subprocess project.
"""
from __future__ import annotations

import json
import keyword
import re
from datetime import datetime

from actions.dev_agent import _generate, _strip_fences, RateLimitError
from core import custom_tools
from core.custom_tools import CustomToolSpec

# A short, real example of the target shape/tone -- trimmed from
# actions/weather_report.py -- included in the prompt so Claude matches
# this project's actual conventions instead of generic Python.
_EXAMPLE_TOOL = '''\
import requests

def weather_report(parameters: dict, player=None, session_memory=None) -> str:
    city = parameters.get("city")
    if not city or not isinstance(city, str) or not city.strip():
        return "Sir, the city is missing for the weather report."
    try:
        resp = requests.get("https://api.example.com/weather", params={"city": city}, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        return f"It's {data[\\'temp\\']} degrees in {city}."
    except Exception as e:
        return f"Could not get the weather for {city}: {e}"
'''

_PROMPT_TEMPLATE = """You are writing ONE new tool module for a Python voice assistant called JARVIS, in Claude's own voice and judgment -- production-quality, no placeholders.

What the user wants: {description}

Every JARVIS tool follows this exact shape:
{example}

Rules for the function you write:
- Signature MUST be exactly: def <tool_name>(parameters: dict, response=None, player=None, session_memory=None) -> str
- Read inputs only from the `parameters` dict (parameters.get(...)).
- NEVER raise past the function -- wrap risky work in try/except and return a plain error string instead.
- Always return a plain string (never None, never a dict/object).
- Only use the Python standard library or these already-installed third-party packages: requests, beautifulsoup4, psutil, pillow, numpy. Do NOT import anything else, and do NOT attempt to pip install anything -- if the capability genuinely needs a package outside that list, instead make the function return a string explaining that and asking the user to install it.
- Do not use exec(), eval(), subprocess, or os.system.
- tool_name must be a short, descriptive, valid Python identifier in snake_case, not colliding with a generic word like "help" or "run".

Also assess, using the same judgment JARVIS's own risk classification already uses elsewhere in this project (real/hard-to-reverse side effects like deleting files, spending money, sending messages, changing system settings = should stay gated behind a confirmation on every future use; read-only or trivially reversible = should not need confirmation every time): should this tool require the user to confirm EACH time it's about to run, going forward?

Reply with ONLY this JSON, no markdown, no explanation:
{{
  "tool_name": "snake_case_name",
  "description": "one plain sentence describing what it does, for both its own tool schema and for JARVIS to say out loud when it's done",
  "parameters_schema": {{"type": "OBJECT", "properties": {{...}}, "required": [...]}},
  "code": "the complete module source, as a single string with \\n for newlines",
  "gated": true or false,
  "gated_reason": "one short phrase why"
}}

JSON:"""


def _is_valid_tool_name(name: str) -> bool:
    return bool(re.fullmatch(r"[a-z][a-z0-9_]{2,48}", name)) and not keyword.iskeyword(name)


def add_capability(parameters: dict, response=None, player=None, session_memory=None, speak=None) -> str:
    def log(msg: str):
        print(f"[SelfExtend] {msg}")
        if player:
            player.write_log(f"[SelfExtend] {msg}")

    params = parameters or {}
    description = (params.get("description") or "").strip()
    if not description:
        return "I need a description of what you want me to add."

    if speak:
        speak("Хорошо, сейчас сделаю.")

    prompt = _PROMPT_TEMPLATE.format(description=description, example=_EXAMPLE_TOOL)

    try:
        text = _generate(prompt, provider="claude", thinking=True)
    except RateLimitError:
        return "Claude's rate limit is up right now -- try again in a minute."
    except Exception as e:
        if "api_key" in str(e).lower() or "authentication" in str(e).lower():
            return "Claude isn't configured yet, so I can't write new capabilities for myself."
        log(f"Generation failed: {e}")
        return f"I couldn't write that: {e}"

    try:
        spec_json = json.loads(_strip_fences(text))
    except json.JSONDecodeError as e:
        log(f"Bad JSON from Claude: {e}\nRaw: {text[:300]}")
        return "I wrote something, but it wasn't valid -- try describing it a bit differently."

    tool_name = str(spec_json.get("tool_name", "")).strip().lower()
    code = spec_json.get("code", "")
    tool_description = str(spec_json.get("description", description))[:200]
    parameters_schema = spec_json.get("parameters_schema") or {"type": "OBJECT", "properties": {}, "required": []}
    gated = bool(spec_json.get("gated", True))

    if not _is_valid_tool_name(tool_name):
        return f"I picked an invalid name ('{tool_name}') for that -- try asking again."
    if custom_tools.tool_exists(tool_name):
        return f"I already have a tool called '{tool_name}' -- ask for this with a more specific description so I pick a different name."
    if not code or f"def {tool_name}(" not in code:
        return "I couldn't write working code for that -- try describing it more concretely."

    custom_tools.CUSTOM_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    source_path = custom_tools.CUSTOM_TOOLS_DIR / f"{tool_name}.py"
    source_path.write_text(code, encoding="utf-8")
    log(f"Wrote {source_path}")

    spec = CustomToolSpec(
        name=tool_name,
        description=tool_description,
        parameters_schema=parameters_schema,
        gated=gated,
        created_at=datetime.now().isoformat(timespec="seconds"),
        source_path=str(source_path),
    )

    try:
        handler = custom_tools.load_module_from_path(tool_name, source_path)
        custom_tools.register_tool(spec, handler)
    except Exception as e:
        log(f"Registration failed: {e}")
        return f"I wrote the code but couldn't add it: {e}"

    custom_tools.save_persisted(spec)
    log(f"Registered '{tool_name}' (gated={gated}).")

    return (
        f"Added a new capability: {tool_name} -- {tool_description} "
        f"It'll be ready the next time we talk."
    )
