"""Public input, upstream response, and per-tool concurrency boundaries."""

import asyncio

import httpx
import pytest

from mcp_gateway import config
from mcp_gateway.limits import ToolInputError, clamp_limit, tool_slot
from mcp_gateway.tools import arxiv as arxiv_mod
from mcp_gateway.tools import kb_search as kb_search_mod
from mcp_gateway.tools import pubmed as pubmed_mod
from mcp_gateway.tools import web_search as web_search_mod
from retrieval.hybrid import HybridResult


class _Client:
    def __init__(self, responses):  # noqa: ANN001
        self.responses = list(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, *_args, **_kwargs):
        return self.responses.pop(0)


def _response(body=b"{}", *, status=200, headers=None):
    return httpx.Response(
        status,
        content=body,
        headers=headers,
        request=httpx.Request("GET", "https://upstream.example/api"),
    )


def test_public_limits_clamp_to_supported_range():
    assert clamp_limit(-100, 5) == 1
    assert clamp_limit(10_000, 5) == 20
    with pytest.raises(ToolInputError, match="integer"):
        clamp_limit(True, 5)


def test_kb_search_rejects_blank_and_oversized_queries_without_retrieval(monkeypatch):
    called = []

    async def fake_hybrid(query, limit):  # noqa: ANN001
        called.append((query, limit))
        return HybridResult([])

    monkeypatch.setattr(kb_search_mod, "hybrid_search", fake_hybrid)

    assert "invalid_query" in asyncio.run(kb_search_mod.kb_search("   "))
    assert "invalid_query" in asyncio.run(
        kb_search_mod.kb_search("x" * (config.QUERY_MAX_CHARS + 1))
    )
    asyncio.run(kb_search_mod.kb_search("bounded", 1000))
    assert called == [("bounded", 20)]


def test_web_search_reports_malformed_and_oversized_responses(monkeypatch):
    monkeypatch.setattr(config, "KAGI_API_KEY", "configured")
    malformed = _Client([_response(b"not-json")])
    monkeypatch.setattr(web_search_mod.httpx, "AsyncClient", lambda **_kw: malformed)
    assert "upstream_malformed" in asyncio.run(web_search_mod.web_search("query"))

    oversized = _Client(
        [_response(headers={"content-length": str(config.UPSTREAM_RESPONSE_MAX_BYTES + 1)})]
    )
    monkeypatch.setattr(web_search_mod.httpx, "AsyncClient", lambda **_kw: oversized)
    assert "upstream_response_too_large" in asyncio.run(
        web_search_mod.web_search("query")
    )


def test_pubmed_and_arxiv_malformed_responses_are_stable_errors(monkeypatch):
    pubmed_client = _Client([_response(b"[]")])
    monkeypatch.setattr(pubmed_mod.httpx, "AsyncClient", lambda **_kw: pubmed_client)
    assert "upstream_malformed" in asyncio.run(pubmed_mod.pubmed_search("query"))

    arxiv_client = _Client([_response(b"<feed>")])
    monkeypatch.setattr(arxiv_mod.httpx, "AsyncClient", lambda **_kw: arxiv_client)
    assert "upstream_malformed" in asyncio.run(arxiv_mod.arxiv_search("query"))


def test_per_tool_concurrency_slot_bounds_active_work():
    active = 0
    peak = 0
    release = asyncio.Event()

    async def worker():
        nonlocal active, peak
        async with tool_slot("test-bounded", 2):
            active += 1
            peak = max(peak, active)
            await release.wait()
            active -= 1

    async def run():
        tasks = [asyncio.create_task(worker()) for _ in range(5)]
        await asyncio.sleep(0)
        assert active == 2
        release.set()
        await asyncio.gather(*tasks)

    asyncio.run(run())
    assert peak == 2


def test_integer_configuration_rejects_invalid_and_out_of_range(monkeypatch):
    monkeypatch.setenv("TEST_LIMIT", "not-an-int")
    with pytest.raises(ValueError, match="integer"):
        config._bounded_int("TEST_LIMIT", 5, 1, 20)
    monkeypatch.setenv("TEST_LIMIT", "21")
    with pytest.raises(ValueError, match="between 1 and 20"):
        config._bounded_int("TEST_LIMIT", 5, 1, 20)


def test_cross_setting_semantics_reject_invalid_chunk_overlap(monkeypatch):
    monkeypatch.setattr(config, "CHUNK_MAX_CHARS", 1000)
    monkeypatch.setattr(config, "CHUNK_OVERLAP_CHARS", 1000)
    with pytest.raises(ValueError, match="smaller"):
        config.validate_runtime_limits()
