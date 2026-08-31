#web_search.py
from pathlib import Path

from core.config import CLAUDE_FAST_MODEL
from core.runtime_config import get_config as _get_config, build_anthropic_client as _build_anthropic_client

CLAUDE_TAG      = "[CLAUDE_ANSWERED]"


def _get_api_key() -> str:
    return _get_config()["gemini_api_key"]


def _get_claude_key() -> str:
    return _get_config().get("claude_api_key", "")


def _gemini_search(query: str) -> str:
    from google import genai

    client   = genai.Client(api_key=_get_api_key())
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=query,
        config={"tools": [{"google_search": {}}]},
    )

    text = ""
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            text += part.text

    text = text.strip()
    if not text:
        raise ValueError("Gemini returned an empty response.")
    return text


def _claude_search(query: str) -> str:
    """Web-grounded answer via Claude's native web search tool."""
    client = _build_anthropic_client()
    msg = client.messages.create(
        model=CLAUDE_FAST_MODEL,
        max_tokens=2048,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": query}],
    )

    text_parts  = []
    sources     = []  # (title, url) — first-seen order, deduped
    seen_urls   = set()
    for block in msg.content:
        if getattr(block, "type", "") != "text":
            continue
        text_parts.append(block.text)
        for citation in (getattr(block, "citations", None) or []):
            url = getattr(citation, "url", None)
            if url and url not in seen_urls:
                seen_urls.add(url)
                sources.append((getattr(citation, "title", "") or url, url))

    text = "".join(text_parts).strip()
    if not text:
        raise ValueError("Claude returned an empty response.")

    if sources:
        text += "\n\nSources:\n" + "\n".join(f"- {title}: {url}" for title, url in sources[:5])

    return text


def _grounded_search(query: str) -> tuple[str, str]:
    """Web-grounded single-shot answer — Claude by default, Gemini as fallback.
    Returns (text, provider_used). Thread-safe: no shared mutable state, so
    concurrent callers (e.g. the news race below) never see each other's provider."""
    if _get_claude_key():
        try:
            return _claude_search(query), "claude"
        except Exception as e:
            print(f"[WebSearch] ⚠️ Claude search failed ({e}) — trying Gemini...")
    return _gemini_search(query), "gemini"


def _ddg_search(query: str, max_results: int = 6) -> list[dict]:
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            results.append({
                "title":   r.get("title",  ""),
                "snippet": r.get("body",   ""),
                "url":     r.get("href",   ""),
            })
    return results


def _ddg_news(query: str, max_results: int = 8) -> list[dict]:
    """DDG news search — returns actual articles, not website homepages."""
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.news(query, max_results=max_results):
                results.append({
                    "title":   r.get("title",  ""),
                    "snippet": r.get("body",   ""),
                    "url":     r.get("url",    ""),
                    "source":  r.get("source", ""),
                })
    except Exception as e:
        print(f"[WebSearch] ⚠️ DDG news() failed ({e}) — falling back to text search")
        results = _ddg_search(query, max_results=max_results)
    return results


def _format_ddg(query: str, results: list[dict]) -> str:
    if not results:
        return f"No results found for: {query}"

    lines = [f"Search results for: {query}\n"]
    for i, r in enumerate(results, 1):
        if r.get("title"):   lines.append(f"{i}. {r['title']}")
        if r.get("snippet"): lines.append(f"   {r['snippet']}")
        if r.get("url"):     lines.append(f"   Source: {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


def _format_news(query: str, results: list[dict]) -> str:
    if not results:
        return f"No news found for: {query}"

    lines = [f"Latest news: {query}\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        if not title:
            continue
        src = f"  [{r['source']}]" if r.get("source") else ""
        lines.append(f"{i}. {title}{src}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet'][:140]}")
        if r.get("url"):
            lines.append(f"   {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


# ── Briefing helper ────────────────────────────────────────────────────────────

def _gemini_headlines(n: int = 5) -> tuple[list[str], str]:
    """
    Fetches current headlines via grounded search (Claude by default, Gemini fallback).
    Optimised for speed: minimal prompt + strict token cap.
    Returns (headline_list, raw_text_for_display).
    """
    import re

    raw, _ = _grounded_search(f"Current world news: {n} headlines. Numbered list, titles only.")

    headlines = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        # Only accept lines that begin with a number — skips preamble/closing sentences
        if not re.match(r'^[\d]+[.\)\-]', line):
            continue
        clean = re.sub(r'^[\d]+[.\)\-]\s*', '', line)
        clean = re.sub(r'^\*+\s*',          '', clean).strip()
        if clean and len(clean) > 10:
            headlines.append(clean)

    return headlines[:n], raw.strip()


# ── Modes ──────────────────────────────────────────────────────────────────────

def _search(query: str) -> str:
    """Default search — grounded (Claude/Gemini), DDG fallback. Returns (text, provider_or_None)."""
    try:
        return _grounded_search(query)
    except Exception as e:
        print(f"[WebSearch] ⚠️ Grounded search failed ({e}) — trying DDG...")
        results = _ddg_search(query)
        return _format_ddg(query, results), None


def _news(query: str) -> tuple[str, str | None]:
    """
    Runs grounded search AND DDG news in parallel.
    Returns whichever delivers a valid result first; the other keeps running
    in the background but is ignored. Returns (text, provider_or_None).
    """
    import threading

    grounded_query = f"latest news today: {query}" if query else "top world news today"
    ddg_query      = query if query else "world news today"

    result_box  = [None]   # (text, provider_or_None) — first valid result lands here
    lock        = threading.Lock()
    done_evt    = threading.Event()
    failures    = [0]

    def _store(text: str, provider: str | None) -> None:
        if text and len(text) > 60:
            with lock:
                if result_box[0] is None:
                    result_box[0] = (text, provider)
            done_evt.set()
        else:
            with lock:
                failures[0] += 1
                if failures[0] >= 2:   # both failed — unblock caller
                    done_evt.set()

    def _try_grounded():
        try:
            text, provider = _grounded_search(grounded_query)
            _store(text, provider)
        except Exception as e:
            print(f"[WebSearch] ⚠️ Grounded news failed ({e})")
            _store("", None)

    def _try_ddg():
        try:
            results = _ddg_news(ddg_query, max_results=8)
            _store(_format_news(ddg_query, results), None)
        except Exception as e:
            print(f"[WebSearch] ⚠️ DDG news failed ({e})")
            _store("", None)

    threading.Thread(target=_try_grounded, daemon=True).start()
    threading.Thread(target=_try_ddg,    daemon=True).start()

    done_evt.wait(timeout=10.0)
    return result_box[0] or (f"No news found for: {query}", None)


def _research(query: str) -> tuple[str, str | None]:
    """
    Deep dive — asks Claude/Gemini for a comprehensive answer with context.
    Falls back to a wider DDG fetch. Returns (text, provider_or_None).
    """
    research_query = (
        f"Comprehensive, detailed explanation of: {query}. "
        "Include background context, key facts, current state, and important nuances."
    )
    try:
        return _grounded_search(research_query)
    except Exception as e:
        print(f"[WebSearch] ⚠️ Research grounded search failed ({e}) — DDG fallback...")
        results = _ddg_search(query, max_results=10)
        return _format_ddg(query, results), None


def _price(query: str) -> tuple[str, str | None]:
    """Product price lookup — searches for current market prices. Returns (text, provider_or_None)."""
    price_query = f"current price of {query} — how much does it cost today"
    try:
        return _grounded_search(price_query)
    except Exception as e:
        print(f"[WebSearch] ⚠️ Price grounded search failed ({e}) — DDG fallback...")
        results = _ddg_search(f"{query} price buy", max_results=6)
        return _format_ddg(query, results), None


def _compare(items: list[str], aspect: str) -> tuple[str, str | None]:
    query = (
        f"Compare {', '.join(items)} in terms of {aspect}. "
        "Give specific facts and data."
    )
    try:
        return _grounded_search(query)
    except Exception as e:
        print(f"[WebSearch] ⚠️ Grounded compare failed: {e} — falling back to DDG")

    all_results: dict[str, list] = {}
    for item in items:
        try:
            all_results[item] = _ddg_search(f"{item} {aspect}", max_results=3)
        except Exception:
            all_results[item] = []

    lines = [f"Comparison — {aspect.upper()}", "─" * 40]
    for item in items:
        lines.append(f"\n▸ {item}")
        for r in all_results.get(item, [])[:2]:
            if r.get("snippet"):
                lines.append(f"  • {r['snippet']}")
            if r.get("url"):
                lines.append(f"    {r['url']}")
    return "\n".join(lines), None


# ── Public entry point ─────────────────────────────────────────────────────────

def web_search(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    query  = params.get("query", "").strip()
    mode   = params.get("mode",  "search").lower().strip()
    items  = params.get("items", [])
    aspect = params.get("aspect", "general").strip() or "general"

    if not query and not items:
        return "Please provide a search query."

    if items and mode not in ("compare",):
        mode = "compare"

    if player:
        player.write_log(f"[Search:{mode}] {query or ', '.join(items)}")

    print(f"[WebSearch] 🔍 mode={mode!r}  query={query!r}")

    try:
        if mode == "compare" and items:
            text, provider = _compare(items, aspect)
        elif mode == "news":
            text, provider = _news(query)
        elif mode == "research":
            text, provider = _research(query)
        elif mode == "price":
            text, provider = _price(query)
        else:
            text, provider = _search(query)

    except Exception as e:
        print(f"[WebSearch] ❌ All backends failed: {e}")
        return f"Search failed: {e}"

    return f"{CLAUDE_TAG} {text}" if provider == "claude" else text
