"""web_search — live web search via the Kagi Search API.

Kept as a separate tool from kb_search on purpose: the model can cross-check the
live web against the offline knowledge base (the "second opinion" pattern). If no
Kagi key is configured, this returns a clear notice rather than failing silently.
Phase 3 can add a SearXNG backend behind this same tool.
"""

from __future__ import annotations

import httpx

from .. import config


async def web_search(query: str, limit: int | None = None) -> str:
    """Search the live web and return cited results."""
    limit = limit or config.WEB_SEARCH_LIMIT
    if not config.KAGI_API_KEY:
        return (
            "web_search is not configured: set KAGI_API_KEY in config/.env "
            "(or wire a SearXNG backend). Falling back to the knowledge base or "
            "another tool may be appropriate."
        )

    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(
                config.KAGI_SEARCH_URL,
                params={"q": query, "limit": str(limit)},
                headers={
                    "Authorization": f"Bot {config.KAGI_API_KEY}",
                    "User-Agent": config.USER_AGENT,
                },
            )
    except httpx.HTTPError as exc:
        return f"web_search error: could not reach Kagi ({exc})."

    if resp.status_code != 200:
        return f"web_search error: Kagi returned HTTP {resp.status_code}: {resp.text[:200]}"

    data = resp.json()
    # Kagi returns {"data": [{"t":0,"title","url","snippet"}, {"t":1,...}], ...}
    # t==0 is a search result; t==1 is a related-searches block we skip.
    results = [r for r in data.get("data", []) if r.get("t") == 0]
    if not results:
        return f'No web results for "{query}".'

    lines = [f'Web results for "{query}":', ""]
    for i, r in enumerate(results[:limit], start=1):
        title = (r.get("title") or "(untitled)").strip()
        url = (r.get("url") or "").strip()
        snippet = (r.get("snippet") or "").strip()
        lines.append(f"{i}. {title}")
        if snippet:
            lines.append(f"   {snippet}")
        lines.append(f"   source: {url}")
        lines.append("")
    lines.append("These are live web results; cite the source URLs above.")
    return "\n".join(lines).rstrip()
