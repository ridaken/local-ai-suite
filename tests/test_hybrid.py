"""Hybrid pipeline tests: merge vector candidates with (stubbed) lexical hits and
apply reranking, with all network dependencies injected/faked."""

import asyncio

from qdrant_client import QdrantClient

from retrieval import hybrid
from retrieval.hybrid import hybrid_search
from retrieval.qdrant_store import ensure_collection, upsert_chunks

DIM = 8


async def fake_embed(_query):
    return [1.0] * DIM


def _seed_client():
    client = QdrantClient(":memory:")
    ensure_collection(client, "las_curated", DIM)
    upsert_chunks(
        client,
        "las_curated",
        [
            ("c1", [1.0] * DIM, {"text": "async functions and the event loop", "path": "a.py",
                                 "symbol": "run", "citation": "a.py:1"}),
            ("c2", [1.0] * DIM, {"text": "unrelated database migration notes", "path": "b.py",
                                 "symbol": "migrate", "citation": "b.py:1"}),
        ],
    )
    return client


def test_hybrid_returns_vector_candidates_and_reranks(monkeypatch):
    # No kiwix in this test: stub it to return nothing.
    async def no_kiwix(_q, _n):
        return []

    monkeypatch.setattr(hybrid, "kiwix_search", no_kiwix)

    # Fake reranker: force c2 ("migrate") ahead of c1 to prove rerank order is applied.
    async def fake_rerank(_query, documents, top_n):
        order = sorted(
            range(len(documents)),
            key=lambda i: 0 if "migration" in documents[i] else 1,
        )
        return [(i, 1.0) for i in order][:top_n]

    client = _seed_client()
    result = asyncio.run(
        hybrid_search(
            "how do async functions work",
            top_k=5,
            embed_fn=fake_embed,
            rerank_fn=fake_rerank,
            client=client,
        )
    )
    assert result.error is None
    assert [c.source for c in result.candidates] == ["curated", "curated"]
    assert result.candidates[0].citation == "b.py:1"  # reranker put migration first
    assert result.candidates[0].title == "migrate"


def test_hybrid_reports_error_when_all_tiers_fail(monkeypatch):
    async def boom_kiwix(_q, _n):
        import httpx

        raise httpx.ConnectError("no server")

    async def boom_embed(_q):
        raise RuntimeError("no embedder")

    monkeypatch.setattr(hybrid, "kiwix_search", boom_kiwix)

    result = asyncio.run(
        hybrid_search("anything", top_k=5, embed_fn=boom_embed, client=QdrantClient(":memory:"))
    )
    assert result.candidates == []
    assert result.error is not None
