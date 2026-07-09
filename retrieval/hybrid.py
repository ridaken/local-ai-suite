"""Hybrid retrieval: merge lexical (Kiwix) + vector (Qdrant) candidates, then
rerank to the final top-k.

Every tier degrades independently — if the vector tier is disabled or the
embedder/reranker/kiwix is unreachable, the pipeline still returns whatever the
surviving tiers produced. Only when *nothing* is available is an error surfaced.

embed_fn / rerank_fn / client are injectable so tests can run the full merge/
rerank logic with a deterministic fake embedder and an in-memory Qdrant.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from mcp_gateway import config, zim_library
from mcp_gateway.settings_store import SettingsStore, default_store

from .embed import embed_query
from .lexical import kiwix_search
from .qdrant_store import get_client, search
from .rerank import rerank

EmbedFn = Callable[[str], Awaitable[list[float]]]
RerankFn = Callable[[str, list[str], int], Awaitable[list[tuple[int, float]]]]


def _enabled_books(settings: SettingsStore) -> list[str] | None:
    """Which kiwix book names the lexical tier should search, per the admin
    UI's per-book toggle. None means "no filter" (pre-Phase-3 behavior,
    including when library.xml hasn't been generated yet)."""
    if not config.LIBRARY_XML_PATH:
        return None
    try:
        installed = zim_library.installed_book_names(config.LIBRARY_XML_PATH)
    except OSError:
        return None
    return settings.enabled_books(installed) if installed else None


@dataclass
class Candidate:
    title: str
    text: str
    citation: str
    source: str  # "kb" (Kiwix) or "curated" (your vectors)
    score: float | None = None


@dataclass
class HybridResult:
    candidates: list[Candidate]
    error: str | None = None
    warning: str | None = None


async def _vector_candidates(query: str, embed_fn: EmbedFn, client) -> list[Candidate]:
    qvec = await embed_fn(query)
    cl = client or get_client(config.QDRANT_URL)
    out = []
    hits = search(cl, config.QDRANT_COLLECTION, qvec, config.HYBRID_VECTOR_CANDIDATES)
    for payload, score in hits:
        out.append(
            Candidate(
                title=payload.get("symbol") or payload.get("path") or "(chunk)",
                text=payload.get("text", ""),
                citation=payload.get("citation") or payload.get("path", ""),
                source="curated",
                score=score,
            )
        )
    return out


async def hybrid_search(
    query: str,
    top_k: int | None = None,
    *,
    embed_fn: EmbedFn | None = None,
    rerank_fn: RerankFn | None = None,
    client=None,
    settings: SettingsStore | None = None,
) -> HybridResult:
    top_k = top_k or config.KB_SEARCH_LIMIT
    settings = settings or default_store()
    mode = settings.get_retrieval_mode()  # "hybrid" | "lexical" | "vector"
    vector_enabled = config.vector_tier_enabled()
    candidates: list[Candidate] = []
    notes: list[str] = []

    if mode in ("hybrid", "vector") and vector_enabled:
        try:
            candidates += await _vector_candidates(query, embed_fn or embed_query, client)
        except Exception as exc:  # noqa: BLE001 - degrade gracefully across any failure
            notes.append(f"vector tier unavailable ({type(exc).__name__})")

    if mode in ("hybrid", "lexical"):
        try:
            books = _enabled_books(settings)
            hits = await kiwix_search(query, config.HYBRID_KIWIX_CANDIDATES, books=books)
            candidates += [
                Candidate(title=h.title, text=h.snippet or h.title, citation=h.url, source="kb")
                for h in hits
            ]
        except Exception as exc:  # noqa: BLE001 - degrade across bad responses too
            notes.append(f"knowledge base unavailable ({type(exc).__name__})")

    if not candidates:
        return HybridResult([], error="; ".join(notes) or None)

    candidates = _dedup(candidates)

    rerank_enabled = settings.get_rerank_enabled() and vector_enabled
    reranked = await _maybe_rerank(
        query, candidates, top_k, rerank_fn or rerank, notes, enabled=rerank_enabled
    )
    return HybridResult(reranked[:top_k], error=None, warning="; ".join(notes) or None)


def _dedup(candidates: list[Candidate]) -> list[Candidate]:
    seen: set[tuple[str, str, str]] = set()
    out = []
    for c in candidates:
        key = (c.source, c.citation, c.text[:80])
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


async def _maybe_rerank(
    query: str,
    candidates: list[Candidate],
    top_k: int,
    rerank_fn: RerankFn,
    notes: list[str],
    *,
    enabled: bool = True,
) -> list[Candidate]:
    if enabled and config.RERANK_URL:
        try:
            order = await rerank_fn(query, [c.text for c in candidates], top_k)
            return [candidates[i] for i, _ in order]
        except Exception as exc:  # noqa: BLE001 - fall back to score ordering
            notes.append(f"reranker unavailable ({type(exc).__name__})")
    # Fallback: vector hits by score first, then lexical hits in their original order.
    scored = sorted(
        (c for c in candidates if c.score is not None), key=lambda c: c.score, reverse=True
    )
    lexical = [c for c in candidates if c.score is None]
    return scored + lexical
