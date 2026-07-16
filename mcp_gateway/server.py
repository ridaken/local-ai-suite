"""local-ai-suite MCP gateway.

The server is passive: it advertises and executes tools while the client drives
the agent loop. Stdio remains the default. LAS_TRANSPORT=http exposes only the
authenticated streamable-HTTP MCP endpoint plus health/readiness probes.
"""

from __future__ import annotations

import contextlib
import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import config
from .limits import tool_slot
from .security import MCPBearerAuthMiddleware
from .settings_store import SettingsStore, set_default_store
from .tools.arxiv import arxiv_search as _arxiv_search
from .tools.compute import calculate as _calculate
from .tools.kb_read import kb_read as _kb_read
from .tools.kb_search import kb_search as _kb_search
from .tools.pubmed import pubmed_search as _pubmed_search
from .tools.web_search import web_search as _web_search


def _transport_security() -> TransportSecuritySettings:
    hosts = config.MCP_ALLOWED_HOSTS
    if "*" in hosts:
        raise ValueError("MCP_ALLOWED_HOSTS may not contain '*'")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"http://{host}" for host in hosts],
    )


mcp = FastMCP("local-ai-suite", transport_security=_transport_security())


@mcp.tool()
async def kb_search(query: str, limit: int = 5) -> str:
    """Search the local offline knowledge base for stable facts and cited passages."""
    async with tool_slot("kb_search", config.KB_SEARCH_CONCURRENCY):
        return await _kb_search(query, limit)


@mcp.tool()
async def kb_read(source: str, offset: int = 0) -> str:
    """Read a knowledge-base article returned by kb_search in paginated windows."""
    async with tool_slot("kb_read", config.KB_READ_CONCURRENCY):
        return await _kb_read(source, offset)


@mcp.tool()
async def web_search(query: str, limit: int = 5) -> str:
    """Search the live web for current information and cited results."""
    async with tool_slot("web_search", config.WEB_SEARCH_CONCURRENCY):
        return await _web_search(query, limit)


@mcp.tool()
async def pubmed_search(query: str, limit: int = 5) -> str:
    """Search PubMed for biomedical literature and cited article summaries."""
    async with tool_slot("pubmed_search", config.PUBMED_SEARCH_CONCURRENCY):
        return await _pubmed_search(query, limit)


@mcp.tool()
async def arxiv_search(query: str, limit: int = 5) -> str:
    """Search arXiv for technical and scientific preprints."""
    async with tool_slot("arxiv_search", config.ARXIV_SEARCH_CONCURRENCY):
        return await _arxiv_search(query, limit)


@mcp.tool()
async def calculate(expression: str) -> str:
    """Evaluate arithmetic and whitelisted common math functions."""
    async with tool_slot("calculate", config.CALCULATE_CONCURRENCY):
        return await _calculate(expression)


def build_app(*, api_key: str | None = None, settings: SettingsStore | None = None) -> Starlette:
    """Build the authenticated MCP-only hosted ASGI application."""
    api_key = api_key if api_key is not None else config.MCP_API_KEY
    if not api_key:
        raise ValueError("MCP_API_KEY is required for HTTP transport")
    settings = settings or SettingsStore(config.SETTINGS_DB, read_only=True, initialize=False)
    set_default_store(settings)
    mcp_app = mcp.streamable_http_app()

    async def healthz(_request):  # noqa: ANN001
        return JSONResponse({"status": "ok"})

    async def readyz(_request):  # noqa: ANN001
        try:
            settings.get_retrieval_mode()
        except Exception as exc:  # noqa: BLE001 - readiness reports unavailable state
            return JSONResponse(
                {"status": "not-ready", "reason": type(exc).__name__}, status_code=503
            )
        return JSONResponse({"status": "ready"})

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        async with mcp.session_manager.run():
            yield

    app = Starlette(
        routes=[Route("/healthz", healthz), Route("/readyz", readyz), *mcp_app.routes],
        lifespan=lifespan,
    )
    app.add_middleware(MCPBearerAuthMiddleware, api_key=api_key)
    return app


def main() -> None:
    transport = os.environ.get("LAS_TRANSPORT", "stdio").strip() or "stdio"
    if transport == "http":
        import uvicorn

        config.validate_http_security(mcp=True)
        uvicorn.run(build_app(), host=config.MCP_HOST, port=config.MCP_PORT)
    else:
        mcp.run(transport=transport)


if __name__ == "__main__":
    main()
