"""Tests for kb_read — URL validation, HTML extraction, and pagination."""

import asyncio

import httpx

from mcp_gateway.tools import kb_read as kb_read_mod
from mcp_gateway.tools.kb_read import content_url, extract_text, kb_read, window

_HTML = """<html><head><title>Hypothyroidism</title>
<style>body { color: red }</style></head>
<body><script>var tracking = 1;</script>
<h1>Hypothyroidism</h1>
<p>First paragraph about symptoms.</p>
<p>Second paragraph about causes &amp; treatment.</p>
</body></html>"""


def _set_kiwix(monkeypatch, url="http://kiwix:8080"):
    monkeypatch.setattr("mcp_gateway.config.KIWIX_URL", url)


# --- content_url: host validation + URL-form mapping ---------------------------


def test_content_url_maps_viewer_fragment_to_content_endpoint(monkeypatch):
    _set_kiwix(monkeypatch)
    url = content_url("http://kiwix:8080/viewer#wikipedia_en/A/Hypothyroidism")
    assert url == "http://kiwix:8080/content/wikipedia_en/A/Hypothyroidism"


def test_content_url_passes_content_path_through(monkeypatch):
    _set_kiwix(monkeypatch)
    url = content_url("http://kiwix:8080/content/wikipedia_en/A/Hypothyroidism")
    assert url == "http://kiwix:8080/content/wikipedia_en/A/Hypothyroidism"


def test_content_url_rejects_foreign_host(monkeypatch):
    # The whole security story: kb_read must not be a generic fetch tool.
    _set_kiwix(monkeypatch)
    assert content_url("http://evil.example/content/wikipedia_en/A/X") is None
    assert content_url("https://kiwix:8080/content/x") is None  # scheme mismatch
    assert content_url("http://kiwix:9999/content/x") is None  # port mismatch


# --- extract_text ---------------------------------------------------------------


def test_extract_text_strips_chrome_and_keeps_paragraphs():
    title, text = extract_text(_HTML)
    assert title == "Hypothyroidism"
    assert "First paragraph about symptoms." in text
    assert "Second paragraph about causes & treatment." in text
    assert "tracking" not in text
    assert "color: red" not in text
    # Block tags become line breaks, not run-together text.
    assert "symptoms.\n" in text or "symptoms.\n\n" in text


# --- window ---------------------------------------------------------------------


def test_window_middle_and_final_pages():
    text = "x" * 100
    body, start, end, total = window(text, 0, 40)
    assert (len(body), start, end, total) == (40, 0, 40, 100)
    body, start, end, total = window(text, 80, 40)
    assert (len(body), start, end, total) == (20, 80, 100, 100)


def test_window_clamps_negative_offset():
    body, start, end, total = window("abcdef", -5, 3)
    assert (body, start, end, total) == ("abc", 0, 3, 6)


# --- kb_read end-to-end (fetch faked) --------------------------------------------


def _fake_fetch(html: str, status_code: int = 200):
    async def fetch(url: str) -> httpx.Response:
        return httpx.Response(status_code, text=html)

    return fetch


def test_kb_read_pages_through_an_article(monkeypatch):
    _set_kiwix(monkeypatch)
    monkeypatch.setattr("mcp_gateway.config.KB_READ_WINDOW_CHARS", 50)
    monkeypatch.setattr(kb_read_mod, "_fetch_html", _fake_fetch(_HTML))
    source = "http://kiwix:8080/viewer#wikipedia_en/A/Hypothyroidism"

    first = asyncio.run(kb_read(source))
    assert '"Hypothyroidism"' in first
    assert "characters 0-50 of" in first
    assert "call kb_read with offset=50 to continue" in first
    assert f"source: {source}" in first

    second = asyncio.run(kb_read(source, offset=50))
    assert "characters 50-" in second


def test_kb_read_final_page_says_end_of_article(monkeypatch):
    _set_kiwix(monkeypatch)
    monkeypatch.setattr("mcp_gateway.config.KB_READ_WINDOW_CHARS", 100_000)
    monkeypatch.setattr(kb_read_mod, "_fetch_html", _fake_fetch(_HTML))

    result = asyncio.run(kb_read("http://kiwix:8080/content/wikipedia_en/A/X"))
    assert "[end of article]" in result


def test_kb_read_offset_past_end(monkeypatch):
    _set_kiwix(monkeypatch)
    monkeypatch.setattr(kb_read_mod, "_fetch_html", _fake_fetch(_HTML))

    result = asyncio.run(
        kb_read("http://kiwix:8080/content/wikipedia_en/A/X", offset=10_000_000)
    )
    assert "past the end" in result


def test_kb_read_refuses_foreign_host_without_fetching(monkeypatch):
    _set_kiwix(monkeypatch)

    async def must_not_fetch(url: str) -> httpx.Response:
        raise AssertionError("foreign host must be rejected before any fetch")

    monkeypatch.setattr(kb_read_mod, "_fetch_html", must_not_fetch)
    result = asyncio.run(kb_read("http://evil.example/content/book/A/X"))
    assert "kb_read error" in result


def test_kb_read_reports_missing_article(monkeypatch):
    _set_kiwix(monkeypatch)
    monkeypatch.setattr(kb_read_mod, "_fetch_html", _fake_fetch("gone", status_code=404))

    result = asyncio.run(kb_read("http://kiwix:8080/content/wikipedia_en/A/Missing"))
    assert "not found" in result
