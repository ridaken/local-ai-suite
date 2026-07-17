"""arxiv_search — live search of arXiv preprints (Atom API).

Live API; nothing stored locally. Returns title, authors, date, abstract snippet
and the arXiv URL for citation.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

import httpx

from .. import config
from ..limits import (
    ToolInputError,
    UpstreamResponseError,
    clamp_limit,
    error_text,
    response_bytes,
    validate_query,
)
from ..schemas import (
    SOURCE_ARXIV,
    SearchResponse,
    SearchResult,
    render_search,
    search_error,
)

_ATOM = "{http://www.w3.org/2005/Atom}"
_WS_RE = re.compile(r"\s+")


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return _WS_RE.sub(" ", text).strip()


async def arxiv_search_response(query: str, limit: int = 5) -> SearchResponse:
    """Search arXiv and return cited preprint summaries."""
    try:
        query = validate_query(query)
        limit = clamp_limit(limit, 5)
    except ToolInputError as exc:
        return search_error(str(query), exc.code, error_text("arxiv_search", exc))
    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(
                config.ARXIV_API_URL,
                params={
                    "search_query": f"all:{query}",
                    "start": "0",
                    "max_results": str(limit),
                    "sortBy": "relevance",
                },
                headers={"User-Agent": config.USER_AGENT},
            )
            resp.raise_for_status()
    except httpx.HTTPError:
        return search_error(
            query,
            "upstream_unavailable",
            "arxiv_search error [upstream_unavailable]: arXiv request failed.",
        )

    try:
        root = ET.fromstring(response_bytes(resp))
    except UpstreamResponseError as exc:
        return search_error(query, exc.code, error_text("arxiv_search", exc))
    except ET.ParseError:
        return search_error(
            query,
            "upstream_malformed",
            "arxiv_search error [upstream_malformed]: arXiv returned malformed XML.",
        )

    results = []
    for entry in root.findall(f"{_ATOM}entry"):
        url = _clean(entry.findtext(f"{_ATOM}id"))
        published = _clean(entry.findtext(f"{_ATOM}published"))[:10]
        authors = [_clean(a.findtext(f"{_ATOM}name")) for a in entry.findall(f"{_ATOM}author")]
        byline = authors[0] + (" et al." if len(authors) > 1 else "") if authors else ""
        summary = _clean(entry.findtext(f"{_ATOM}summary"))
        if len(summary) > 300:
            summary = summary[:297] + "..."
        meta = ", ".join(x for x in [byline, published] if x)
        results.append(
            SearchResult(
                id=f"arxiv:{url}" if url else "",
                title=_clean(entry.findtext(f"{_ATOM}title")) or "(untitled)",
                excerpt="\n   ".join(x for x in [meta, summary] if x),
                source_kind=SOURCE_ARXIV,
                citation=url,
            )
        )
    return SearchResponse(query=query, results=results)


def render(response: SearchResponse) -> str:
    return render_search(
        response,
        heading="arXiv results",
        footer="These are arXiv preprints; cite the URLs above.",
    )


async def arxiv_search(query: str, limit: int = 5) -> str:
    """Text-only entry point (stdio clients and tests)."""
    return render(await arxiv_search_response(query, limit))
