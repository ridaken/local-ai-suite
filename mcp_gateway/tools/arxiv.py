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

_ATOM = "{http://www.w3.org/2005/Atom}"
_WS_RE = re.compile(r"\s+")


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return _WS_RE.sub(" ", text).strip()


async def arxiv_search(query: str, limit: int = 5) -> str:
    """Search arXiv and return cited preprint summaries."""
    try:
        query = validate_query(query)
        limit = clamp_limit(limit, 5)
    except ToolInputError as exc:
        return error_text("arxiv_search", exc)
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
        return "arxiv_search error [upstream_unavailable]: arXiv request failed."

    try:
        root = ET.fromstring(response_bytes(resp))
    except UpstreamResponseError as exc:
        return error_text("arxiv_search", exc)
    except ET.ParseError:
        return "arxiv_search error [upstream_malformed]: arXiv returned malformed XML."

    entries = root.findall(f"{_ATOM}entry")
    if not entries:
        return f'No arXiv results for "{query}".'

    lines = [f'arXiv results for "{query}":', ""]
    for i, entry in enumerate(entries, start=1):
        title = _clean(entry.findtext(f"{_ATOM}title")) or "(untitled)"
        url = _clean(entry.findtext(f"{_ATOM}id"))
        published = _clean(entry.findtext(f"{_ATOM}published"))[:10]
        authors = [
            _clean(a.findtext(f"{_ATOM}name"))
            for a in entry.findall(f"{_ATOM}author")
        ]
        byline = authors[0] + (" et al." if len(authors) > 1 else "") if authors else ""
        summary = _clean(entry.findtext(f"{_ATOM}summary"))
        if len(summary) > 300:
            summary = summary[:297] + "..."
        lines.append(f"{i}. {title}")
        meta = ", ".join(x for x in [byline, published] if x)
        if meta:
            lines.append(f"   {meta}")
        if summary:
            lines.append(f"   {summary}")
        lines.append(f"   source: {url}")
        lines.append("")
    lines.append("These are arXiv preprints; cite the URLs above.")
    return "\n".join(lines).rstrip()
