"""pubmed_search — live biomedical literature search via NCBI E-utilities.

Two hops: esearch (query -> PMIDs) then esummary (PMIDs -> citation metadata).
Live API, so results are always current and nothing is stored locally. An API
key + email are optional but raise the rate limit.
"""

from __future__ import annotations

import httpx

from .. import config


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
            ids = search.json().get("esearchresult", {}).get("idlist", [])
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
    except httpx.HTTPError as exc:
        return f"pubmed_search error: could not reach NCBI E-utilities ({exc})."

    result = summary.json().get("result", {})
    lines = [f'PubMed results for "{query}":', ""]
    for i, pmid in enumerate(ids, start=1):
        doc = result.get(pmid, {})
        title = (doc.get("title") or "(untitled)").strip().rstrip(".")
        journal = (doc.get("source") or "").strip()
        pubdate = (doc.get("pubdate") or "").strip()
        authors = doc.get("authors") or []
        first_author = authors[0].get("name", "") if authors else ""
        byline = ", ".join(x for x in [first_author + (" et al." if len(authors) > 1 else ""), journal, pubdate] if x)
        lines.append(f"{i}. {title}")
        if byline:
            lines.append(f"   {byline}")
        lines.append(f"   source: https://pubmed.ncbi.nlm.nih.gov/{pmid}/")
        lines.append("")
    lines.append("These are PubMed citations; cite the PMIDs / URLs above.")
    return "\n".join(lines).rstrip()
