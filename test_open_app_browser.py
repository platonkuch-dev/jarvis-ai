"""Focused tests for browser and website handling in open_app."""
from __future__ import annotations

from actions import open_app as module


def check(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"[PASS] {label}")


def test_website_alias() -> None:
    original = module.webbrowser.open
    opened: list[tuple[str, int]] = []
    try:
        module.webbrowser.open = lambda url, new=0: opened.append((url, new)) or True
        result = module.open_app({"app_name": "youtube"})
        check("website alias opens its destination URL", opened == [("https://www.youtube.com", 2)])
        check("website alias reports success", result == "Opened https://www.youtube.com.")
    finally:
        module.webbrowser.open = original


def test_browser_start_page() -> None:
    original = module._open_browser_start_page
    try:
        calls: list[str] = []
        module._open_browser_start_page = lambda browser: calls.append(browser) or True
        result = module.open_app({"app_name": "Chrome"})
        check("Chrome is given a non-blank start page", calls == ["chrome"])
        check("browser start reports its destination", result == "Opened Chrome at Google.")
    finally:
        module._open_browser_start_page = original


test_website_alias()
test_browser_start_page()