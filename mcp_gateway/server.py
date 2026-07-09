"""local-ai-suite MCP gateway.

A single passive MCP server that advertises tools and executes them. The model
(driven by the client harness — pi, or OpenWebUI via mcpo) decides which tool to
call. Run over stdio by default (works for both pi and mcpo):

    python -m mcp_gateway.server

Set LAS_TRANSPORT=http to serve MCP-over-streamable-HTTP *and* the admin UI in
one process (the management plane — see build_app()); LAS_TRANSPORT=sse for
FastMCP's native SSE transport with no admin UI.

Tool docstrings below double as the descriptions the model sees, so they are
written to help it choose the right tool.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette

from . import config, zim_library
from .admin import build_admin_app
from .downloads import DownloadManager
from .settings_store import default_store
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


def build_app() -> Starlette:
    """The management-plane app: MCP over streamable HTTP at /mcp, plus the
    admin UI at /, merged into one Starlette app / one uvicorn process.

    Routes are merged (not Mount-nested) so both FastMCP's session-manager
    lifespan and the admin routes run in a single ASGI app without path
    duplication — mcp.streamable_http_app() already serves its route at
    settings.streamable_http_path (default "/mcp").
    """
    settings = default_store()

    def _refresh_library() -> None:
        if config.ZIM_DIR and config.LIBRARY_XML_PATH:
            zim_library.refresh_library(Path(config.ZIM_DIR), Path(config.LIBRARY_XML_PATH))

    # Pick up any ZIMs already sitting in ZIM_DIR (pre-existing from Phase 1/2,
    # or dropped in by hand) immediately, rather than waiting for the next
    # add/delete through the admin UI.
    _refresh_library()

    download_manager = DownloadManager(config.ZIM_DIR or ".", on_complete=_refresh_library)
    admin_app = build_admin_app(
        settings=settings,
        download_manager=download_manager,
        zim_dir=config.ZIM_DIR,
        library_xml_path=config.LIBRARY_XML_PATH,
    )
    mcp_app = mcp.streamable_http_app()

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        async with mcp.session_manager.run():
            yield

    return Starlette(routes=[*admin_app.routes, *mcp_app.routes], lifespan=lifespan)


def main() -> None:
    transport = os.environ.get("LAS_TRANSPORT", "stdio").strip() or "stdio"
    if transport == "http":
        import uvicorn

        uvicorn.run(build_app(), host=config.ADMIN_HOST, port=config.ADMIN_PORT)
    else:
        mcp.run(transport=transport)


if __name__ == "__main__":
    main()
