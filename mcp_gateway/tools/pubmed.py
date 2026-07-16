"""pubmed_search — live biomedical literature search via NCBI E-utilities.

Two hops: esearch (query -> PMIDs) then esummary (PMIDs -> citation metadata).
Live API, so results are always current and nothing is stored locally. An API
key + email are optional but raise the rate limit.
"""

from __future__ import annotations

import httpx

from .. import config
from ..limits import (
    ToolInputError,
    UpstreamResponseError,
    clamp_limit,
    error_text,
    response_json,
    validate_query,
)


def _auth_params() -> dict[str, str]:
    params = {"tool": config.NCBI_TOOL}
    if config.NCBI_EMAIL:
        params["email"] = config.NCBI_EMAIL
    if config.NCBI_API_KEY:
        params["api_key"] = config.NCBI_API_KEY
    return params


async def pubmed_search(query: str, limit: int = 5) -> str:
    """Search PubMed and return cited article summaries."""
    try:
        query = validate_query(query)
        limit = clamp_limit(limit, 5)
    except ToolInputError as exc:
        return error_text("pubmed_search", exc)
    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT, follow_redirects=True) as client:
            headers = {"User-Agent": config.USER_AGENT}
            search = await client.get(
                f"{config.NCBI_BASE}/esearch.fcgi",
                params={
                    "db": "pubmed",
                    "term": query,
                    "retmax": str(limit),
                    "retmode": "json",
                    "sort": "relevance",
                    **_auth_params(),
                },
                headers=headers,
            )
            search.raise_for_status()
            search_data = response_json(search)
            if not isinstance(search_data, dict):
                raise UpstreamResponseError("upstream_malformed", "NCBI returned an invalid shape")
            search_result = search_data.get("esearchresult")
            if not isinstance(search_result, dict):
                raise UpstreamResponseError(
                    "upstream_malformed", "NCBI returned an invalid search result"
                )
            ids = search_result.get("idlist", [])
            if not isinstance(ids, list) or not all(isinstance(value, str) for value in ids):
                raise UpstreamResponseError("upstream_malformed", "NCBI returned invalid IDs")
            if not ids:
                return f'No PubMed results for "{query}".'

            summary = await client.get(
                f"{config.NCBI_BASE}/esummary.fcgi",
                params={
                    "db": "pubmed",
                    "id": ",".join(ids),
                    "retmode": "json",
                    **_auth_params(),
                },
                headers=headers,
            )
            summary.raise_for_status()
    except httpx.HTTPError:
        return "pubmed_search error [upstream_unavailable]: NCBI request failed."
    except UpstreamResponseError as exc:
        return error_text("pubmed_search", exc)

    try:
        summary_data = response_json(summary)
        if not isinstance(summary_data, dict):
            raise UpstreamResponseError("upstream_malformed", "NCBI returned an invalid shape")
        result = summary_data.get("result", {})
        if not isinstance(result, dict):
            raise UpstreamResponseError("upstream_malformed", "NCBI returned invalid summaries")
        if any(not isinstance(result.get(pmid, {}), dict) for pmid in ids):
            raise UpstreamResponseError("upstream_malformed", "NCBI returned invalid summaries")
    except UpstreamResponseError as exc:
        return error_text("pubmed_search", exc)
    lines = [f'PubMed results for "{query}":', ""]
    try:
        for i, pmid in enumerate(ids, start=1):
            doc = result.get(pmid, {})
            title = (doc.get("title") or "(untitled)").strip().rstrip(".")
            journal = (doc.get("source") or "").strip()
            pubdate = (doc.get("pubdate") or "").strip()
            authors = doc.get("authors") or []
            if not isinstance(authors, list) or not all(
                isinstance(author, dict) for author in authors
            ):
                raise TypeError("invalid authors")
            first_author = authors[0].get("name", "") if authors else ""
            if first_author and len(authors) > 1:
                first_author += " et al."
            byline = ", ".join(x for x in [first_author, journal, pubdate] if x)
            lines.append(f"{i}. {title}")
            if byline:
                lines.append(f"   {byline}")
            lines.append(f"   source: https://pubmed.ncbi.nlm.nih.gov/{pmid}/")
            lines.append("")
    except (AttributeError, TypeError):
        malformed = UpstreamResponseError("upstream_malformed", "NCBI returned invalid summaries")
        return error_text("pubmed_search", malformed)
    lines.append("These are PubMed citations; cite the PMIDs / URLs above.")
    return "\n".join(lines).rstrip()
