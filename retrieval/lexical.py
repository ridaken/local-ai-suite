"""Lexical retrieval over the offline knowledge base (kiwix-serve full-text search).

This is the Phase 1 mechanism, factored out so both the kb_search tool and the
Phase 2 hybrid pipeline can use it without a circular import.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

import httpx

from mcp_gateway import config

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass
class Hit:
    title: str
    url: str
    snippet: str


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()


def _absolute(url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return f"{config.KIWIX_URL}/{url.lstrip('/')}"


async def kiwix_search(query: str, limit: int) -> list[Hit]:
    """Query kiwix-serve full-text search. Raises httpx.HTTPError if unreachable."""
    params = {"pattern": query, "pageLength": str(limit), "format": "xml"}
    if config.KIWIX_BOOK:
        params["books.name"] = config.KIWIX_BOOK

    async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(
            f"{config.KIWIX_URL}/search",
            params=params,
            headers={"User-Agent": config.USER_AGENT},
        )
    resp.raise_for_status()

    root = ET.fromstring(resp.content)
    hits: list[Hit] = []
    for item in root.findall(".//item")[:limit]:
        hits.append(
            Hit(
                title=_clean(item.findtext("title")) or "(untitled)",
                url=_absolute(_clean(item.findtext("link"))),
                snippet=_clean(item.findtext("description")),
            )
        )
    return hits
