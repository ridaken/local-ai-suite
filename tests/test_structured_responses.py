"""Structured MCP responses: every tool advertises an output schema and returns
structuredContent alongside a readable text fallback.

These go through mcp.call_tool (not the tool functions directly) because the
structured/unstructured split is produced by the FastMCP result conversion —
testing the inner function would not prove a client sees either half.
"""

import asyncio

import pytest

from mcp_gateway import server
from mcp_gateway.schemas import CalculationResponse, ReadResponse, SearchResponse
from retrieval.hybrid import Candidate, HybridResult

_SEARCH_TOOLS = ["kb_search", "web_search", "pubmed_search", "arxiv_search"]


def _tool(name):
    tools = asyncio.run(server.mcp.list_tools())
    return next(t for t in tools if t.name == name)


@pytest.mark.parametrize("name", [*_SEARCH_TOOLS, "kb_read", "calculate"])
def test_every_tool_advertises_an_output_schema(name):
    assert _tool(name).outputSchema is not None


@pytest.mark.parametrize("name", _SEARCH_TOOLS)
def test_search_tools_share_the_standard_response_shape(name):
    properties = _tool(name).outputSchema["properties"]
    assert set(properties) >= {"query", "results", "warnings", "retrieved_at"}


@pytest.mark.parametrize("name", [*_SEARCH_TOOLS, "kb_read"])
def test_tool_descriptions_mark_sources_untrusted(name):
    assert "untrusted" in (_tool(name).description or "").lower()


def test_kb_search_returns_structured_content_and_text(monkeypatch):
    async def fake_hybrid(_query, _limit, **_kw):
        return HybridResult(
            [
                Candidate(
                    title="Async",
                    text="the event loop runs coroutines",
                    citation="http://kiwix:8080/viewer#b/A/Async",
                    source="kb",
                    id="kb:http://kiwix:8080/viewer#b/A/Async",
                    rerank_score=0.87,
                )
            ],
            warning="vector tier unavailable (ConnectError)",
        )

    monkeypatch.setattr(server.kb_search_mod, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(server.config, "KB_SEARCH_FETCH_EXCERPTS", False)

    result = asyncio.run(server.mcp.call_tool("kb_search", {"query": "async", "limit": 3}))

    structured = result.structuredContent
    assert structured["query"] == "async"
    assert structured["warnings"] == ["vector tier unavailable (ConnectError)"]
    assert structured["retrieved_at"]
    # Validates against the published schema.
    parsed = SearchResponse.model_validate(structured)
    assert parsed.results[0].id == "kb:http://kiwix:8080/viewer#b/A/Async"
    assert parsed.results[0].source_kind == "kb"
    assert parsed.results[0].citation.endswith("/A/Async")
    assert parsed.results[0].rerank_score == 0.87

    text = result.content[0].text
    assert "the event loop runs coroutines" in text
    assert "untrusted source material" in text
    assert not result.isError


def test_kb_search_input_error_is_typed_and_flagged():
    result = asyncio.run(server.mcp.call_tool("kb_search", {"query": "   "}))
    parsed = SearchResponse.model_validate(result.structuredContent)
    assert parsed.error is not None
    assert parsed.error.code == "invalid_query"
    assert parsed.results == []
    assert result.isError


def test_kb_read_returns_paging_fields(monkeypatch):
    async def fake_read(source, offset=0):
        return ReadResponse(
            title="Async",
            source=source,
            text="body text",
            offset=offset,
            next_offset=9,
            total_length=100,
            end_of_document=False,
        )

    monkeypatch.setattr(server.kb_read_mod, "kb_read_response", fake_read)
    result = asyncio.run(
        server.mcp.call_tool("kb_read", {"source": "http://kiwix:8080/content/b/A/Async"})
    )
    parsed = ReadResponse.model_validate(result.structuredContent)
    assert (parsed.offset, parsed.next_offset, parsed.total_length) == (0, 9, 100)
    assert parsed.end_of_document is False
    assert "body text" in result.content[0].text


def test_kb_read_end_of_document_has_no_next_offset(monkeypatch):
    async def fake_read(source, offset=0):
        return ReadResponse(
            title="Async",
            source=source,
            text="tail",
            offset=offset,
            next_offset=None,
            total_length=4,
            end_of_document=True,
        )

    monkeypatch.setattr(server.kb_read_mod, "kb_read_response", fake_read)
    result = asyncio.run(server.mcp.call_tool("kb_read", {"source": "http://kiwix:8080/x"}))
    parsed = ReadResponse.model_validate(result.structuredContent)
    assert parsed.next_offset is None
    assert parsed.end_of_document is True
    assert "[end of article]" in result.content[0].text


def test_calculate_returns_structured_result():
    result = asyncio.run(server.mcp.call_tool("calculate", {"expression": "2 + 2"}))
    parsed = CalculationResponse.model_validate(result.structuredContent)
    assert parsed.expression == "2 + 2"
    assert parsed.result == "2 + 2 = 4"
    assert parsed.error is None
    assert not result.isError


def test_calculate_error_is_typed():
    result = asyncio.run(server.mcp.call_tool("calculate", {"expression": "2 +"}))
    parsed = CalculationResponse.model_validate(result.structuredContent)
    assert parsed.error is not None
    assert parsed.error.code == "invalid_expression"
    assert result.isError
