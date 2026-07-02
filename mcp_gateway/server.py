"""local-ai-suite MCP gateway.

A single passive MCP server that advertises tools and executes them. The model
(driven by the client harness — pi, or OpenWebUI via mcpo) decides which tool to
call. Run over stdio by default (works for both pi and mcpo):

    python -m mcp_gateway.server

Set LAS_TRANSPORT=sse or streamable-http to serve over HTTP instead.

Tool docstrings below double as the descriptions the model sees, so they are
written to help it choose the right tool.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from .tools.arxiv import arxiv_search as _arxiv_search
from .tools.compute import calculate as _calculate
from .tools.kb_search import kb_search as _kb_search
from .tools.pubmed import pubmed_search as _pubmed_search
from .tools.web_search import web_search as _web_search

mcp = FastMCP("local-ai-suite")


@mcp.tool()
async def kb_search(query: str, limit: int = 5) -> str:
    """Search the local offline knowledge base (e.g. Wikipedia) for factual,
    encyclopedic, or reference information. Use this first for background facts,
    definitions, and established knowledge — it works without internet and is a
    good second opinion to cross-check web results. Returns passages with source
    URLs to cite."""
    return await _kb_search(query, limit)


@mcp.tool()
async def web_search(query: str, limit: int = 5) -> str:
    """Search the live web for current events, recent information, or anything
    that may have changed recently or is not in the offline knowledge base.
    Returns titles, snippets, and URLs to cite. Prefer kb_search for stable,
    encyclopedic facts; use this when freshness matters."""
    return await _web_search(query, limit)


@mcp.tool()
async def pubmed_search(query: str, limit: int = 5) -> str:
    """Search PubMed for biomedical and clinical literature (papers, trials,
    reviews). Use for medical, health, biology, or life-sciences questions that
    call for peer-reviewed sources. Returns citations with PMIDs and URLs."""
    return await _pubmed_search(query, limit)


@mcp.tool()
async def arxiv_search(query: str, limit: int = 5) -> str:
    """Search arXiv for preprints in physics, math, computer science, and related
    fields. Use for academic, technical, or cutting-edge research questions.
    Returns titles, authors, abstract snippets, and URLs to cite."""
    return await _arxiv_search(query, limit)


@mcp.tool()
async def calculate(expression: str) -> str:
    """Evaluate a mathematical expression precisely (arithmetic and common math
    functions like sqrt, sin, log, factorial). Use this instead of doing
    arithmetic yourself whenever accuracy matters. Example: 'sqrt(2) * 3 + 10'."""
    return await _calculate(expression)


def main() -> None:
    transport = os.environ.get("LAS_TRANSPORT", "stdio").strip() or "stdio"
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
