import asyncio

import pytest

from tools import web


def test_html_to_text_keeps_content_and_drops_noise():
    html = """<html><head><title> Курс  валют </title><style>.x{color:red}</style></head>
    <body><nav>Меню Главная</nav><h1>Доллар</h1><p>Курс ЦБ: 84,41&nbsp;₽</p>
    <script>alert('x')</script><ul><li>Евро</li><li>Юань</li></ul><footer>© 2026</footer></body></html>"""
    title, text = web.html_to_text(html)
    assert title == "Курс валют"
    assert "Доллар" in text and "84,41" in text and "Евро\nЮань" in text
    assert "alert" not in text and "Меню" not in text and "color" not in text and "2026" not in text


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8765/",
    "http://localhost/",
    "http://192.168.1.1/",
    "file:///C:/Windows/win.ini",
    "ftp://example.com/",
])
def test_local_and_odd_addresses_are_refused(url):
    result = asyncio.run(web._read_webpage(url=url))
    assert result["status"] == "error"
