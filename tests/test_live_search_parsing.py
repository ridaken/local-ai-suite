"""Happy-path parsing for the live search tools.

test_tool_limits covers the failure modes (malformed, oversized, unreachable);
these cover the other half: a well-formed upstream response must land in the
standard SearchResult fields — id, citation, source kind — since clients now
consume those instead of parsing prose.
"""

import asyncio
import json

import httpx

from mcp_gateway import config
from mcp_gateway.tools import arxiv as arxiv_mod
from mcp_gateway.tools import pubmed as pubmed_mod
from mcp_gateway.tools import web_search as web_search_mod


class _Client:
    def __init__(self, responses):  # noqa: ANN001
        self.responses = list(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, *_args, **_kwargs):
        return self.responses.pop(0)


def _response(body: bytes):
    return httpx.Response(
        200, content=body, request=httpx.Request("GET", "https://upstream.example/api")
    )


def test_web_search_parses_kagi_results(monkeypatch):
    monkeypatch.setattr(config, "KAGI_API_KEY", "configured")
    body = json.dumps(
        {
            "data": [
                {"t": 0, "title": "Result", "url": "https://a.example/x", "snippet": "answer"},
                {"t": 1, "list": ["related"]},  # related-searches block: skipped
            ]
        }
    ).encode()
    monkeypatch.setattr(
        web_search_mod.httpx, "AsyncClient", lambda **_kw: _Client([_response(body)])
    )

    response = asyncio.run(web_search_mod.web_search_response("query"))

    assert response.error is None
    assert len(response.results) == 1
    result = response.results[0]
    assert result.id == "web:https://a.example/x"
    assert result.source_kind == "web"
    assert result.citation == "https://a.example/x"
    assert result.excerpt == "answer"
    text = web_search_mod.render(response)
    assert "https://a.example/x" in text
    assert "untrusted source material" in text


def test_pubmed_parses_esearch_then_esummary(monkeypatch):
    esearch = json.dumps({"esearchresult": {"idlist": ["12345"]}}).encode()
    esummary = json.dumps(
        {
            "result": {
                "12345": {
                    "title": "Thyroid study.",
                    "source": "J Endo",
                    "pubdate": "2024 Jan",
                    "authors": [{"name": "Smith A"}, {"name": "Jones B"}],
                }
            }
        }
    ).encode()
    monkeypatch.setattr(
        pubmed_mod.httpx,
        "AsyncClient",
        lambda **_kw: _Client([_response(esearch), _response(esummary)]),
    )

    response = asyncio.run(pubmed_mod.pubmed_search_response("thyroid"))

    assert response.error is None
    result = response.results[0]
    assert result.id == "pubmed:12345"
    assert result.source_kind == "pubmed"
    assert result.citation == "https://pubmed.ncbi.nlm.nih.gov/12345/"
    assert result.title == "Thyroid study"
    assert "Smith A et al." in result.excerpt


def test_pubmed_no_hits_is_empty_not_error(monkeypatch):
    esearch = json.dumps({"esearchresult": {"idlist": []}}).encode()
    monkeypatch.setattr(
        pubmed_mod.httpx, "AsyncClient", lambda **_kw: _Client([_response(esearch)])
    )
    response = asyncio.run(pubmed_mod.pubmed_search_response("nothing"))
    assert response.error is None
    assert response.results == []


def test_arxiv_parses_atom_entries(monkeypatch):
    atom = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.00001</id>
    <title>Attention Everywhere</title>
    <published>2024-01-02T00:00:00Z</published>
    <summary>We study attention.</summary>
    <author><name>A. Author</name></author>
    <author><name>B. Author</name></author>
  </entry>
</feed>"""
    monkeypatch.setattr(
        arxiv_mod.httpx, "AsyncClient", lambda **_kw: _Client([_response(atom)])
    )

    response = asyncio.run(arxiv_mod.arxiv_search_response("attention"))

    assert response.error is None
    result = response.results[0]
    assert result.id == "arxiv:http://arxiv.org/abs/2401.00001"
    assert result.source_kind == "arxiv"
    assert result.citation == "http://arxiv.org/abs/2401.00001"
    assert result.title == "Attention Everywhere"
    assert "A. Author et al." in result.excerpt
    assert "2024-01-02" in result.excerpt
