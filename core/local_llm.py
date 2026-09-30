"""Best-effort local chat fallback powered by a running Ollama server."""
from __future__ import annotations

import json
import os
from urllib.error import URLError
from urllib.request import Request, urlopen


def answer(prompt: str, timeout: float = 20.0, model: str | None = None) -> str | None:
    endpoint = os.getenv("JARVIS_OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
    model = model or os.getenv("JARVIS_OLLAMA_MODEL", "qwen2.5:7b")
    body = json.dumps({
        "model": model,
        "prompt": f"You are JARVIS. Reply in the user's language, briefly and helpfully. User: {prompt}",
        "stream": False,
        "options": {"temperature": 0.4, "num_predict": 220},
    }).encode("utf-8")
    request = Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
        text = str(result.get("response", "")).strip()
        return text or None
    except (URLError, TimeoutError, OSError, ValueError):
        return None