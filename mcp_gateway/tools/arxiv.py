"""arxiv_search — live search of arXiv preprints (Atom API).

Live API; nothing stored locally. Returns title, authors, date, abstract snippet
and the arXiv URL for citation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit
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


@dataclass(frozen=True)
class ArxivRecord:
    arxiv_id: str
    title: str
    abstract: str
    authors: tuple[str, ...]
    published: str
    citation: str


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return _WS_RE.sub(" ", text).strip()


def _id_from_url(url: str) -> str:
    path = urlsplit(url).path.strip("/")
    return path.removeprefix("abs/")


def parse_arxiv_feed(payload: bytes) -> list[ArxivRecord]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise UpstreamResponseError(
            "upstream_malformed", "arXiv returned malformed XML"
        ) from exc
    records = []
    for entry in root.findall(f"{_ATOM}entry"):
        url = _clean(entry.findtext(f"{_ATOM}id"))
        arxiv_id = _id_from_url(url)
        if not arxiv_id:
            continue
        records.append(
            ArxivRecord(
                arxiv_id=arxiv_id,
                title=_clean(entry.findtext(f"{_ATOM}title")) or "(untitled)",
                abstract=_clean(entry.findtext(f"{_ATOM}summary")),
                authors=tuple(
                    _clean(author.findtext(f"{_ATOM}name"))
                    for author in entry.findall(f"{_ATOM}author")
                    if _clean(author.findtext(f"{_ATOM}name"))
                ),
                published=_clean(entry.findtext(f"{_ATOM}published"))[:10],
                citation=url,
            )
        )
    return records


async def fetch_arxiv_record(client: httpx.AsyncClient, arxiv_id: str) -> ArxivRecord | None:
    response = await client.get(
        config.ARXIV_API_URL,
        params={"id_list": arxiv_id, "max_results": "1"},
        headers={"User-Agent": config.USER_AGENT},
    )
    response.raise_for_status()
    records = parse_arxiv_feed(response_bytes(response))
    return records[0] if records else None


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
        records = parse_arxiv_feed(response_bytes(resp))
    except UpstreamResponseError as exc:
        return search_error(query, exc.code, error_text("arxiv_search", exc))

    results = []
    for record in records:
        byline = (
            record.authors[0] + (" et al." if len(record.authors) > 1 else "")
            if record.authors
            else ""
        )
        meta = ", ".join(x for x in [byline, record.published] if x)
        results.append(
            SearchResult(
                id=f"arxiv:{record.citation}",
                article_id=f"arxiv:{record.arxiv_id}",
                title=record.title,
                excerpt=meta,
                abstract=record.abstract or None,
                available_content=["metadata", "abstract", "full_text"],
                source_kind=SOURCE_ARXIV,
                citation=record.citation,
            )
        )
    return SearchResponse(query=query, results=results)


def render(response: SearchResponse) -> str:
    return render_search(
        response,
        heading="arXiv results",
        footer=(
            "Search results are candidates, not proof. Select relevant article_id values and "
            "call article_find/article_read before citing paper contents."
        ),
    )


async def arxiv_search(query: str, limit: int = 5) -> str:
    """Text-only entry point (stdio clients and tests)."""
    return render(await arxiv_search_response(query, limit))
