"""Embedding client — calls llama-server's OpenAI-compatible /v1/embeddings.

Kept as a thin async function so ingest and query paths share one code path, and
so tests can inject a deterministic fake embedder instead.
"""

from __future__ import annotations

import httpx

from mcp_gateway import config


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts. Returns one vector per input, in order."""
    if not texts:
        return []
    async with httpx.AsyncClient(timeout=max(config.HTTP_TIMEOUT, 60.0)) as client:
        resp = await client.post(
            config.EMBED_URL,
            json={"model": config.EMBED_MODEL, "input": texts},
            headers={"User-Agent": config.USER_AGENT},
        )
    resp.raise_for_status()
    data = resp.json()["data"]
    # OpenAI shape returns items with an "index"; sort to guarantee input order.
    ordered = sorted(data, key=lambda d: d.get("index", 0))
    return [item["embedding"] for item in ordered]


async def embed_query(text: str) -> list[float]:
    return (await embed_texts([text]))[0]
