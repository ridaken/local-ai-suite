"""Hybrid pipeline tests: merge vector candidates with (stubbed) lexical hits and
apply reranking, with all network dependencies injected/faked."""

import asyncio

from qdrant_client import QdrantClient

from mcp_gateway.settings_store import SettingsStore
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


def test_hybrid_returns_vector_candidates_and_reranks(monkeypatch, tmp_path):
    # No kiwix in this test: stub it to return nothing.
    async def no_kiwix(_q, _n, books=None):
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
            settings=SettingsStore(tmp_path / "settings.db"),
        )
    )
    assert result.error is None
    assert [c.source for c in result.candidates] == ["curated", "curated"]
    assert result.candidates[0].citation == "b.py:1"  # reranker put migration first
    assert result.candidates[0].title == "migrate"


def test_hybrid_reports_error_when_all_tiers_fail(monkeypatch, tmp_path):
    async def boom_kiwix(_q, _n, books=None):
        import httpx

        raise httpx.ConnectError("no server")

    async def boom_embed(_q):
        raise RuntimeError("no embedder")

    monkeypatch.setattr(hybrid, "kiwix_search", boom_kiwix)

    result = asyncio.run(
        hybrid_search(
            "anything",
            top_k=5,
            embed_fn=boom_embed,
            client=QdrantClient(":memory:"),
            settings=SettingsStore(tmp_path / "settings.db"),
        )
    )
    assert result.candidates == []
    assert result.error is not None


def test_hybrid_warns_when_one_tier_fails_but_results_survive(monkeypatch, tmp_path):
    async def boom_kiwix(_q, _n, books=None):
        raise ValueError("bad xml")

    monkeypatch.setattr(hybrid, "kiwix_search", boom_kiwix)

    settings = SettingsStore(tmp_path / "settings.db")
    settings.set_rerank_enabled(False)
    client = _seed_client()
    result = asyncio.run(
        hybrid_search(
            "anything",
            top_k=5,
            embed_fn=fake_embed,
            client=client,
            settings=settings,
        )
    )
    assert result.candidates
    assert result.error is None
    assert result.warning == "knowledge base unavailable (ValueError)"


def test_lexical_mode_skips_vector_tier(monkeypatch, tmp_path):
    async def no_kiwix(_q, _n, books=None):
        return []

    monkeypatch.setattr(hybrid, "kiwix_search", no_kiwix)

    async def boom_embed(_q):
        raise AssertionError("vector tier should not be queried in lexical mode")

    settings = SettingsStore(tmp_path / "settings.db")
    settings.set_retrieval_mode("lexical")

    result = asyncio.run(
        hybrid_search(
            "anything",
            top_k=5,
            embed_fn=boom_embed,
            client=QdrantClient(":memory:"),
            settings=settings,
        )
    )
    # No candidates from either tier (kiwix stubbed empty, vector never called) -> no error either
    # since both tiers "ran" without failing, just returned nothing.
    assert result.candidates == []


def test_vector_mode_skips_lexical_tier(monkeypatch, tmp_path):
    async def boom_kiwix(_q, _n, books=None):
        raise AssertionError("lexical tier should not be queried in vector mode")

    monkeypatch.setattr(hybrid, "kiwix_search", boom_kiwix)

    settings = SettingsStore(tmp_path / "settings.db")
    settings.set_retrieval_mode("vector")

    client = _seed_client()
    result = asyncio.run(
        hybrid_search(
            "how do async functions work",
            top_k=5,
            embed_fn=fake_embed,
            client=client,
            settings=settings,
        )
    )
    assert result.error is None
    assert all(c.source == "curated" for c in result.candidates)


def test_rerank_disabled_falls_back_to_score_order(monkeypatch, tmp_path):
    async def no_kiwix(_q, _n, books=None):
        return []

    monkeypatch.setattr(hybrid, "kiwix_search", no_kiwix)

    async def boom_rerank(_query, _documents, _top_n):
        raise AssertionError("reranker should not be called when rerank is disabled")

    settings = SettingsStore(tmp_path / "settings.db")
    settings.set_rerank_enabled(False)

    client = _seed_client()
    result = asyncio.run(
        hybrid_search(
            "how do async functions work",
            top_k=5,
            embed_fn=fake_embed,
            rerank_fn=boom_rerank,
            client=client,
            settings=settings,
        )
    )
    assert result.error is None
    assert len(result.candidates) == 2


def test_enabled_books_are_passed_to_kiwix_search(monkeypatch, tmp_path):
    seen_books = []

    async def spy_kiwix(_q, _n, books=None):
        seen_books.append(books)
        return []

    monkeypatch.setattr(hybrid, "kiwix_search", spy_kiwix)

    library_path = tmp_path / "library.xml"
    library_path.write_text(
        '<?xml version="1.0"?><library>'
        '<book id="1" name="wikipedia_en_all" path="/data/a.zim"/>'
        '<book id="2" name="stackoverflow" path="/data/b.zim"/>'
        "</library>",
        encoding="utf-8",
    )
    monkeypatch.setattr(hybrid.config, "LIBRARY_XML_PATH", str(library_path))

    settings = SettingsStore(tmp_path / "settings.db")
    settings.set_book_enabled("stackoverflow", False)

    asyncio.run(
        hybrid_search(
            "anything",
            top_k=5,
            client=QdrantClient(":memory:"),
            settings=settings,
        )
    )
    assert seen_books == [["wikipedia_en_all"]]
