"""Tests for the Kiwix lexical search wrapper."""

import asyncio

import httpx

from retrieval import lexical

_SEARCH_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Search: black hole</title>
  <item><title>Black hole</title><link>/content/b/A/Black_hole</link>
    <description>A black hole is a region of spacetime.</description></item>
</channel></rss>"""

# Kiwix highlights matched terms with <b>, so <description> has child elements.
_HIGHLIGHTED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Search: symptoms of hypothyroidism</title>
  <item><title>Hypothyroidism</title><link>/content/b/A/Hypothyroidism</link>
    <description>...<b>of</b> hypothyroidism include tiredness, weight gain,
    dry skin, and feeling <b>cold</b>.</description></item>
</channel></rss>"""


def _capture_transport():
    """An httpx transport that records the request and returns fixed search XML."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = request.url
        return httpx.Response(200, text=_SEARCH_XML)

    return httpx.MockTransport(handler), seen


def _run_with_transport(monkeypatch, coro_factory):
    transport, seen = _capture_transport()
    orig = lexical.httpx.AsyncClient

    def make_client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(lexical.httpx, "AsyncClient", make_client)
    result = asyncio.run(coro_factory())
    return result, seen


def test_kiwix_search_empty_book_allowlist_returns_no_hits(monkeypatch):
    class _NoHttpClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("empty allowlist should not call kiwix")

    monkeypatch.setattr(lexical.httpx, "AsyncClient", _NoHttpClient)

    assert asyncio.run(lexical.kiwix_search("anything", 5, books=[])) == []


def test_kiwix_search_uses_books_filter_name_param(monkeypatch):
    # Regression: kiwix-serve's search filter key is `books.filter.name`, not
    # `books.name`. The wrong key returns HTTP 400 (raising), silently breaking
    # every filtered kb_search in the deployed gateway.
    hits, seen = _run_with_transport(
        monkeypatch,
        lambda: lexical.kiwix_search("black hole", 5, books=["book_a", "book_b"]),
    )

    params = seen["url"].params
    assert "books.name" not in params
    assert params.get_list("books.filter.name") == ["book_a", "book_b"]
    assert len(hits) == 1 and hits[0].title == "Black hole"


def test_kiwix_search_captures_highlighted_snippet_text(monkeypatch):
    # Regression: kiwix wraps matched terms in <b>, so findtext("description")
    # returned only the leading "..." (text before the first child), leaving the
    # model with empty snippets. The full snippet text must survive.
    transport, _seen = _capture_transport()

    def handler(request):
        return httpx.Response(200, text=_HIGHLIGHTED_XML)

    transport = httpx.MockTransport(handler)
    orig = lexical.httpx.AsyncClient
    monkeypatch.setattr(
        lexical.httpx,
        "AsyncClient",
        lambda *a, **k: orig(*a, **{**k, "transport": transport}),
    )

    hits = asyncio.run(lexical.kiwix_search("symptoms of hypothyroidism", 5))

    assert len(hits) == 1
    snippet = hits[0].snippet
    assert "hypothyroidism include tiredness" in snippet
    assert "feeling cold" in snippet
    assert "<b>" not in snippet  # tags stripped
    assert snippet != "..."  # the exact old-bug symptom


def test_kiwix_search_builds_citation_on_public_host(monkeypatch):
    # Citation URLs must use the browser-reachable public base, not the internal
    # service name the gateway fetches through.
    monkeypatch.setattr(lexical.config, "KIWIX_URL", "http://kiwix:8080")
    monkeypatch.setattr(lexical.config, "KIWIX_PUBLIC_URL", "http://localhost:8080")

    def handler(request):
        return httpx.Response(200, text=_SEARCH_XML)

    transport = httpx.MockTransport(handler)
    orig = lexical.httpx.AsyncClient
    monkeypatch.setattr(
        lexical.httpx, "AsyncClient", lambda *a, **k: orig(*a, **{**k, "transport": transport})
    )

    hits = asyncio.run(lexical.kiwix_search("black hole", 5))
    assert hits[0].url == "http://localhost:8080/content/b/A/Black_hole"


def test_kiwix_search_single_book_config_uses_filter_name(monkeypatch):
    monkeypatch.setattr(lexical.config, "KIWIX_BOOK", "solo_book")
    _hits, seen = _run_with_transport(
        monkeypatch,
        lambda: lexical.kiwix_search("q", 5, books=None),
    )

    params = seen["url"].params
    assert "books.name" not in params
    assert params.get("books.filter.name") == "solo_book"
