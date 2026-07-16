"""Reranker client — calls llama-server's /v1/rerank (bge-reranker-v2-m3).

Reranking is the highest-leverage quality step: it reorders the merged lexical +
vector candidates by true relevance to the query. Returns (index, score) pairs
sorted best-first, referencing positions in the input documents list.
"""

from __future__ import annotations

import httpx

from mcp_gateway import config
from mcp_gateway.limits import UpstreamResponseError, response_json


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
    body = response_json(resp)
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        raise UpstreamResponseError("upstream_malformed", "reranker returned an invalid shape")
    try:
        pairs = [(r["index"], r.get("relevance_score", 0.0)) for r in body["results"]]
    except (AttributeError, KeyError, TypeError) as exc:
        raise UpstreamResponseError(
            "upstream_malformed", "reranker returned invalid results"
        ) from exc
    if any(not isinstance(index, int) or not 0 <= index < len(documents) for index, _ in pairs):
        raise UpstreamResponseError("upstream_malformed", "reranker returned invalid indexes")
    if any(not isinstance(score, (int, float)) for _index, score in pairs):
        raise UpstreamResponseError("upstream_malformed", "reranker returned invalid scores")
    pairs.sort(key=lambda p: p[1], reverse=True)
    return pairs[:top_n]
