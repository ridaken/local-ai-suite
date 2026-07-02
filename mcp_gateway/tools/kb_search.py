"""kb_search — full-text search over the offline knowledge base (kiwix-serve).

Phase 1 is Kiwix full-text search only (no embeddings). Phase 2 upgrades this to
hybrid retrieval (Kiwix FTS + Qdrant vectors, then rerank). The public function
signature is kept stable so the upgrade is transparent to callers.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

import httpx

from .. import config

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()


def _absolute(url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return f"{config.KIWIX_URL}/{url.lstrip('/')}"


async def kb_search(query: str, limit: int | None = None) -> str:
    """Search the offline knowledge base and return cited passages."""
    limit = limit or config.KB_SEARCH_LIMIT
    params = {"pattern": query, "pageLength": str(limit), "format": "xml"}
    if config.KIWIX_BOOK:
        params["books.name"] = config.KIWIX_BOOK

    url = f"{config.KIWIX_URL}/search"
    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(
                url, params=params, headers={"User-Agent": config.USER_AGENT}
            )
    except httpx.HTTPError as exc:
        return (
            f"kb_search error: could not reach kiwix-serve at {config.KIWIX_URL} "
            f"({exc}). Is the container running (docker compose up) and are ZIMs "
            f"present in ZIM_DIR?"
        )

    if resp.status_code != 200:
        return (
            f"kb_search error: kiwix-serve returned HTTP {resp.status_code}. "
            f"If this is a 400, no books may be loaded yet — add a ZIM to ZIM_DIR."
        )

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        return f"kb_search error: could not parse kiwix search response ({exc})."

    # kiwix-serve returns OpenSearch RSS: <rss><channel><item>...</item></channel>.
    items = root.findall(".//item")
    if not items:
        return f'No results in the local knowledge base for "{query}".'

    lines = [f'Local knowledge base results for "{query}":', ""]
    for i, item in enumerate(items[:limit], start=1):
        title = _clean(item.findtext("title")) or "(untitled)"
        link = _absolute(_clean(item.findtext("link")))
        snippet = _clean(item.findtext("description"))
        lines.append(f"{i}. {title}")
        if snippet:
            lines.append(f"   {snippet}")
        lines.append(f"   source: {link}")
        lines.append("")
    lines.append(
        "These are offline knowledge-base excerpts; cite the source URLs above."
    )
    return "\n".join(lines).rstrip()
