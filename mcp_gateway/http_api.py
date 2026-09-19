"""Authenticated JSON API over the same typed operations exposed through MCP.

MCP is the preferred agent interface.  These routes exist for scripts, test
harnesses, and applications that only speak ordinary HTTP.  They deliberately
call the same response functions as MCP so evidence levels, citations, limits,
and errors cannot diverge between clients.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import BaseModel, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import config
from .limits import tool_slot
from .schemas import (
    ArticlePassageResponse,
    ArticleReadResponse,
    CalculationResponse,
    ReadResponse,
    SearchResponse,
    ToolError,
)
from .tools import article as article_mod
from .tools import arxiv as arxiv_mod
from .tools import compute as compute_mod
from .tools import kb_read as kb_read_mod
from .tools import kb_search as kb_search_mod
from .tools import pubmed as pubmed_mod
from .tools import web_search as web_search_mod


class SearchRequest(BaseModel):
    query: str
    limit: int = Field(default=5, description="Requested results; clamped to 1..20")


class ReadRequest(BaseModel):
    source: str
    offset: int = 0


class ArticleFindRequest(BaseModel):
    article_id: str
    query: str
    limit: int = Field(default=5, description="Requested passages; clamped to 1..20")


class ArticleReadRequest(BaseModel):
    article_id: str
    offset: int = 0


class CalculateRequest(BaseModel):
    expression: str


@dataclass(frozen=True)
class Operation:
    name: str
    path: str
    summary: str
    request_model: type[BaseModel]
    response_model: type[BaseModel]
    call: Callable[[BaseModel], Awaitable[BaseModel]]


async def _kb_search(payload: BaseModel) -> SearchResponse:
    data = SearchRequest.model_validate(payload)
    async with tool_slot("kb_search", config.KB_SEARCH_CONCURRENCY):
        return await kb_search_mod.kb_search_response(data.query, data.limit)


async def _kb_read(payload: BaseModel) -> ReadResponse:
    data = ReadRequest.model_validate(payload)
    async with tool_slot("kb_read", config.KB_READ_CONCURRENCY):
        return await kb_read_mod.kb_read_response(data.source, data.offset)


async def _web_search(payload: BaseModel) -> SearchResponse:
    data = SearchRequest.model_validate(payload)
    async with tool_slot("web_search", config.WEB_SEARCH_CONCURRENCY):
        return await web_search_mod.web_search_response(data.query, data.limit)


async def _pubmed_search(payload: BaseModel) -> SearchResponse:
    data = SearchRequest.model_validate(payload)
    async with tool_slot("pubmed_search", config.PUBMED_SEARCH_CONCURRENCY):
        return await pubmed_mod.pubmed_search_response(data.query, data.limit)


async def _arxiv_search(payload: BaseModel) -> SearchResponse:
    data = SearchRequest.model_validate(payload)
    async with tool_slot("arxiv_search", config.ARXIV_SEARCH_CONCURRENCY):
        return await arxiv_mod.arxiv_search_response(data.query, data.limit)


async def _article_find(payload: BaseModel) -> ArticlePassageResponse:
    data = ArticleFindRequest.model_validate(payload)
    async with tool_slot("article_find", config.ARTICLE_FIND_CONCURRENCY):
        return await article_mod.article_find_response(data.article_id, data.query, data.limit)


async def _article_read(payload: BaseModel) -> ArticleReadResponse:
    data = ArticleReadRequest.model_validate(payload)
    async with tool_slot("article_read", config.ARTICLE_READ_CONCURRENCY):
        return await article_mod.article_read_response(data.article_id, data.offset)


async def _calculate(payload: BaseModel) -> CalculationResponse:
    data = CalculateRequest.model_validate(payload)
    async with tool_slot("calculate", config.CALCULATE_CONCURRENCY):
        return await compute_mod.calculate_response(data.expression)


OPERATIONS = (
    Operation(
        "kb_search",
        "/api/v1/kb/search",
        "Search the local knowledge base",
        SearchRequest,
        SearchResponse,
        _kb_search,
    ),
    Operation(
        "kb_read",
        "/api/v1/kb/read",
        "Read a local knowledge-base article",
        ReadRequest,
        ReadResponse,
        _kb_read,
    ),
    Operation(
        "web_search",
        "/api/v1/web/search",
        "Search the configured live web provider",
        SearchRequest,
        SearchResponse,
        _web_search,
    ),
    Operation(
        "pubmed_search",
        "/api/v1/pubmed/search",
        "Search PubMed for article candidates",
        SearchRequest,
        SearchResponse,
        _pubmed_search,
    ),
    Operation(
        "arxiv_search",
        "/api/v1/arxiv/search",
        "Search arXiv for article candidates",
        SearchRequest,
        SearchResponse,
        _arxiv_search,
    ),
    Operation(
        "article_find",
        "/api/v1/articles/find",
        "Find evidence passages in a selected article",
        ArticleFindRequest,
        ArticlePassageResponse,
        _article_find,
    ),
    Operation(
        "article_read",
        "/api/v1/articles/read",
        "Read sequential context from a selected article",
        ArticleReadRequest,
        ArticleReadResponse,
        _article_read,
    ),
    Operation(
        "calculate",
        "/api/v1/calculate",
        "Evaluate a bounded arithmetic expression",
        CalculateRequest,
        CalculationResponse,
        _calculate,
    ),
)


def _error_status(error: ToolError | None) -> int:
    if error is None:
        return 200
    if error.code.startswith("invalid_") or error.code.startswith("offset_"):
        return 400
    if error.code == "not_configured":
        return 503
    if error.code == "content_unavailable":
        return 404
    if error.code.startswith("upstream_"):
        return 502
    return 422


def _endpoint(operation: Operation):  # noqa: ANN202
    async def endpoint(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("request body must be a JSON object")
            payload = operation.request_model.model_validate(body)
        except (ValueError, ValidationError) as exc:
            error = ToolError(code="invalid_request", message=f"Invalid request: {exc}")
            return JSONResponse({"error": error.model_dump(mode="json")}, status_code=400)
        response = await operation.call(payload)
        return JSONResponse(
            response.model_dump(mode="json"),
            status_code=_error_status(getattr(response, "error", None)),
        )

    endpoint.__name__ = f"api_{operation.name}"
    return endpoint


def _schemas() -> dict[str, dict]:
    models = {
        model
        for operation in OPERATIONS
        for model in (operation.request_model, operation.response_model)
    }
    components: dict[str, dict] = {}
    for model in models:
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        components.update(schema.pop("$defs", {}))
        components[model.__name__] = schema
    return components


async def api_index(_request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "name": "local-ai-suite",
            "api_version": "v1",
            "authentication": "Authorization: Bearer <MCP_API_KEY>",
            "openapi": "/api/v1/openapi.json",
            "mcp": "/mcp",
            "research_workflow": [
                "Search PubMed or arXiv for candidates.",
                "Select one or more article_id values.",
                "Call articles/find for claim-specific passages.",
                "Call articles/read at returned offsets when more context is needed.",
                "Use content_level to distinguish full_text, abstract, and metadata evidence.",
            ],
            "operations": [
                {"name": item.name, "method": "POST", "path": item.path}
                for item in OPERATIONS
            ],
        }
    )


async def openapi(_request: Request) -> JSONResponse:
    paths = {}
    for operation in OPERATIONS:
        paths[operation.path] = {
            "post": {
                "operationId": operation.name,
                "summary": operation.summary,
                "security": [{"bearerAuth": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "$ref": (
                                    "#/components/schemas/"
                                    f"{operation.request_model.__name__}"
                                )
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Typed tool response",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": (
                                        "#/components/schemas/"
                                        f"{operation.response_model.__name__}"
                                    )
                                }
                            }
                        },
                    },
                    "400": {"description": "Invalid request"},
                    "401": {"description": "Missing or invalid bearer token"},
                    "502": {"description": "Upstream provider failure"},
                    "503": {"description": "Optional provider not configured"},
                },
            }
        }
    return JSONResponse(
        {
            "openapi": "3.1.0",
            "info": {"title": "local-ai-suite API", "version": "1.0.0"},
            "paths": paths,
            "components": {
                "securitySchemes": {
                    "bearerAuth": {"type": "http", "scheme": "bearer"}
                },
                "schemas": _schemas(),
            },
        }
    )


def api_routes() -> list[Route]:
    return [
        Route("/api/v1", api_index, methods=["GET"]),
        Route("/api/v1/openapi.json", openapi, methods=["GET"]),
        *(Route(item.path, _endpoint(item), methods=["POST"]) for item in OPERATIONS),
    ]
