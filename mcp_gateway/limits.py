"""Shared public-input, upstream-body, and concurrency boundaries."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

import httpx

from . import config


class ToolInputError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class UpstreamResponseError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_query(query: str) -> str:
    if not isinstance(query, str) or not query.strip():
        raise ToolInputError("invalid_query", "query must not be blank")
    if len(query) > config.QUERY_MAX_CHARS:
        raise ToolInputError(
            "invalid_query", f"query exceeds {config.QUERY_MAX_CHARS} characters"
        )
    return query.strip()


def clamp_limit(value: int | None, default: int) -> int:
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolInputError("invalid_limit", "limit must be an integer")
    return min(20, max(1, value))


def validate_offset(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolInputError("invalid_offset", "offset must be an integer")
    if value < 0 or value > config.ARTICLE_MAX_OFFSET:
        raise ToolInputError(
            "invalid_offset",
            f"offset must be between 0 and {config.ARTICLE_MAX_OFFSET}",
        )
    return value


def validate_source(source: str) -> str:
    if not isinstance(source, str) or not source.strip():
        raise ToolInputError("invalid_source", "source must not be blank")
    if len(source) > 8192:
        raise ToolInputError("invalid_source", "source exceeds 8192 characters")
    return source.strip()


def error_text(tool: str, exc: ToolInputError | UpstreamResponseError) -> str:
    return f"{tool} error [{exc.code}]: {exc}"


def response_bytes(resp: httpx.Response, *, maximum: int | None = None) -> bytes:
    maximum = maximum or config.UPSTREAM_RESPONSE_MAX_BYTES
    length = resp.headers.get("content-length", "")
    if length.isdigit() and int(length) > maximum:
        raise UpstreamResponseError(
            "upstream_response_too_large", f"upstream response exceeds {maximum} bytes"
        )
    content = resp.content
    if len(content) > maximum:
        raise UpstreamResponseError(
            "upstream_response_too_large", f"upstream response exceeds {maximum} bytes"
        )
    return content


def response_json(resp: httpx.Response, *, maximum: int | None = None) -> Any:
    try:
        return json.loads(response_bytes(resp, maximum=maximum))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise UpstreamResponseError(
            "upstream_malformed", "upstream returned malformed JSON"
        ) from exc


_SEMAPHORES: dict[tuple[int, str, int], asyncio.Semaphore] = {}


@asynccontextmanager
async def tool_slot(name: str, limit: int):
    """Bound in-flight work per tool and per event loop."""
    loop = asyncio.get_running_loop()
    key = (id(loop), name, limit)
    semaphore = _SEMAPHORES.setdefault(key, asyncio.Semaphore(limit))
    await semaphore.acquire()
    try:
        yield
    finally:
        semaphore.release()
