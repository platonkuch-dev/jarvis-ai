"""
Process-wide cached reader for config/api_keys.json.

Before this module existed, ~19 separate files across actions/*.py,
memory/*.py, and integrations/*.py each independently opened, read, and
JSON-parsed this file from disk on EVERY call — meaning a single voice
command that touches a couple of tools could mean several redundant
disk reads + JSON parses of a file that essentially never changes during
a running session.

get_config() loads once and serves the in-memory dict on every call after
that — no filesystem access at all on a cache hit. An mtime-check-per-call
design was tried first and measured: on this filesystem, Path.stat() costs
nearly as much as the full read+json.loads (~0.05ms vs ~0.07ms), so
"cheaper" staleness checking on every call barely saves anything over just
re-reading outright. Correctness for the one real mutation path (the user
reconfiguring an API key) instead comes from every writer calling
invalidate_config_cache() explicitly right after it saves — see
ui.py's prompt_reconfig()/wait_for_api_key() save paths, and the two
save-then-invalidate sites in actions/screen_processor.py and
integrations/telegram_login.py.
"""
from __future__ import annotations

import json
import threading

from core.path_utils import get_config_path

_lock = threading.Lock()
_cache: dict | None = None


def get_config() -> dict:
    """Returns the SHARED cached dict — callers that need to modify it
    (e.g. before writing an updated config back to disk) must copy it
    first (`dict(get_config())`), never mutate the return value in place."""
    global _cache
    if _cache is not None:
        return _cache
    with _lock:
        if _cache is None:
            try:
                _cache = json.loads(get_config_path().read_text(encoding="utf-8"))
            except Exception:
                _cache = {}
        return _cache


def invalidate_config_cache() -> None:
    """Call right after writing a new api_keys.json (reconfiguration, a
    tool saving a derived setting like camera_index, etc.) so the NEXT
    get_config() call re-reads instead of serving the stale in-memory copy.
    Without this, a value change would only become visible after a restart."""
    global _cache
    with _lock:
        _cache = None


def get_gemini_api_key() -> str:
    return get_config().get("gemini_api_key", "")


def get_claude_api_key() -> str:
    return get_config().get("claude_api_key", "")


def get_anthropic_workspace_id() -> str:
    return get_config().get("anthropic_workspace_id", "")


def build_anthropic_client():
    """Constructs an `anthropic.Anthropic` client with the correct header
    for an identity-linked API key. Anthropic API keys minted for a specific
    workspace (as opposed to a standard account-level key) reject requests
    that don't carry an `anthropic-workspace-id` header naming that
    workspace — a real 400 error hit live in production
    ("anthropic-workspace-id is required when authenticating with an
    identity-linked API key") from actions/web_search.py, which silently
    fell back to Gemini instead of surfacing the failure. ~12 call sites
    across actions/*.py independently constructed `anthropic.Anthropic(
    api_key=...)` with no header, all carrying the same latent bug — use
    this instead of constructing the client directly."""
    import anthropic
    kwargs: dict = {"api_key": get_claude_api_key()}
    workspace_id = get_anthropic_workspace_id()
    if workspace_id:
        kwargs["default_headers"] = {"anthropic-workspace-id": workspace_id}
    return anthropic.Anthropic(**kwargs)
