"""Tests for the Kiwix lexical search wrapper."""

import asyncio

from retrieval import lexical


def test_kiwix_search_empty_book_allowlist_returns_no_hits(monkeypatch):
    class _NoHttpClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("empty allowlist should not call kiwix")

    monkeypatch.setattr(lexical.httpx, "AsyncClient", _NoHttpClient)

    assert asyncio.run(lexical.kiwix_search("anything", 5, books=[])) == []
