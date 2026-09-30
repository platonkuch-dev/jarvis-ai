"""Tests the Ollama fallback protocol without contacting a real server."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.local_llm import answer


with patch("core.local_llm.urlopen") as open_url:
    response = MagicMock()
    response.read.return_value = b'{"response":"Local response"}'
    open_url.return_value.__enter__.return_value = response
    assert answer("hello", model="qwen2.5:7b") == "Local response"
    assert b'"model": "qwen2.5:7b"' in open_url.call_args.args[0].data
    print("[PASS] Ollama response is parsed")

with patch("core.local_llm.urlopen", side_effect=OSError("offline")):
    assert answer("hello") is None
    print("[PASS] unavailable Ollama falls back cleanly")