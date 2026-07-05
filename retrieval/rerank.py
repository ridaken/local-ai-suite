"""Reranker client — calls llama-server's /v1/rerank (bge-reranker-v2-m3).

Reranking is the highest-leverage quality step: it reorders the merged lexical +
vector candidates by true relevance to the query. Returns (index, score) pairs
sorted best-first, referencing positions in the input documents list.
"""

from __future__ import annotations

import httpx

from mcp_gateway import config


async def rerank(query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
    """Rerank documents against the query. Raises httpx.HTTPError on failure."""
    if not documents:
        return []
    async with httpx.AsyncClient(timeout=max(config.HTTP_TIMEOUT, 60.0)) as client:
        resp = await client.post(
            config.RERANK_URL,
            json={
                "model": config.RERANK_MODEL,
                "query": query,
                "documents": documents,
                "top_n": top_n,
            },
            headers={"User-Agent": config.USER_AGENT},
        )
    resp.raise_for_status()
    results = resp.json()["results"]
    pairs = [(r["index"], r.get("relevance_score", 0.0)) for r in results]
    pairs.sort(key=lambda p: p[1], reverse=True)
    return pairs[:top_n]
