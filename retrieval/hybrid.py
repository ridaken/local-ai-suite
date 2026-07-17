"""Hybrid retrieval: run lexical (Kiwix) + vector (Qdrant) concurrently, fuse the
two independent rankings with reciprocal-rank fusion, then rerank to the final
top-k.

Every tier degrades independently — if the vector tier is disabled or the
embedder/reranker/kiwix is unreachable, the pipeline still returns whatever the
surviving tiers produced. Only when *nothing* is available is an error surfaced.

The two tiers score on incomparable scales (cosine similarity vs. kiwix's
ranking), so they are never compared numerically. RRF fuses them by *rank*,
which is what keeps a degraded run useful: with one tier down the fusion order
collapses to the surviving tier's own order rather than to an arbitrary
"all vector hits first" split.

embed_fn / rerank_fn / client are injectable so tests can run the full merge/
rerank logic with a deterministic fake embedder and an in-memory Qdrant.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from mcp_gateway import config, zim_library
from mcp_gateway.limits import clamp_limit, validate_query
from mcp_gateway.settings_store import SettingsStore, default_store

from .embed import embed_query
from .lexical import kiwix_search
from .qdrant_store import get_client, search
from .rerank import rerank

EmbedFn = Callable[[str], Awaitable[list[float]]]
RerankFn = Callable[[str, list[str], int], Awaitable[list[tuple[int, float]]]]

# Reciprocal-rank-fusion damping. 60 is the value from the original RRF paper and
# the de-facto default; it flattens the head of each list enough that one tier's
# rank-1 hit cannot automatically win the fused ordering.
_RRF_K = 60


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
    score: float | None = None  # raw tier score; None for lexical (kiwix ranks, not scores)
    id: str = ""
    corpus_version: str | None = None
    rerank_score: float | None = None
    fusion_score: float | None = None
    _tier_rank: int = field(default=0, repr=False, compare=False)


@dataclass
class HybridResult:
    candidates: list[Candidate]
    error: str | None = None
    warning: str | None = None


async def _vector_candidates(query: str, embed_fn: EmbedFn, client) -> list[Candidate]:
    qvec = await embed_fn(query)
    cl = client or get_client(config.QDRANT_URL)
    # qdrant_client is synchronous; keep it off the event loop so a slow vector
    # search cannot stall the concurrent lexical tier or other in-flight tools.
    hits = await asyncio.to_thread(
        search, cl, config.QDRANT_COLLECTION, qvec, config.HYBRID_VECTOR_CANDIDATES
    )
    out = []
    for payload, score in hits:
        chunk_id = payload.get("chunk_id") or payload.get("citation") or ""
        out.append(
            Candidate(
                title=payload.get("symbol") or payload.get("path") or "(chunk)",
                text=payload.get("text", ""),
                citation=payload.get("citation") or payload.get("path", ""),
                source="curated",
                score=score,
                id=f"curated:{chunk_id}" if chunk_id else "",
                corpus_version=payload.get("corpus_version"),
            )
        )
    return out


async def _lexical_candidates(query: str, settings: SettingsStore) -> list[Candidate]:
    books = _enabled_books(settings)
    hits = await kiwix_search(query, config.HYBRID_KIWIX_CANDIDATES, books=books)
    return [
        Candidate(
            title=h.title,
            text=h.snippet or h.title,
            citation=h.url,
            source="kb",
            id=f"kb:{h.url}" if h.url else "",
        )
        for h in hits
    ]


async def hybrid_search(
    query: str,
    top_k: int | None = None,
    *,
    embed_fn: EmbedFn | None = None,
    rerank_fn: RerankFn | None = None,
    client=None,
    settings: SettingsStore | None = None,
) -> HybridResult:
    query = validate_query(query)
    top_k = clamp_limit(top_k, config.KB_SEARCH_LIMIT)
    settings = settings or default_store()
    mode = settings.get_retrieval_mode()  # "hybrid" | "lexical" | "vector"
    vector_enabled = config.vector_tier_enabled()
    notes: list[str] = []

    # Both tiers are independent network round-trips; overlap them rather than
    # paying vector latency + lexical latency in series.
    want_vector = mode in ("hybrid", "vector") and vector_enabled
    want_lexical = mode in ("hybrid", "lexical")
    jobs: list[tuple[str, Awaitable[list[Candidate]]]] = []
    if want_vector:
        jobs.append(("vector tier", _vector_candidates(query, embed_fn or embed_query, client)))
    if want_lexical:
        jobs.append(("knowledge base", _lexical_candidates(query, settings)))

    settled = await asyncio.gather(*(coro for _label, coro in jobs), return_exceptions=True)

    ranked_lists: list[list[Candidate]] = []
    survived = 0
    for (label, _coro), outcome in zip(jobs, settled, strict=True):
        if isinstance(outcome, BaseException):
            notes.append(f"{label} unavailable ({type(outcome).__name__})")
            continue
        survived += 1
        ranked_lists.append(_dedup(outcome))

    if not any(ranked_lists):
        # A tier that ran and found nothing is not an outage. Only report an
        # error when nothing was able to answer at all — otherwise a query with
        # genuinely no matches looks like a broken deployment.
        if survived:
            return HybridResult([], error=None, warning="; ".join(notes) or None)
        return HybridResult([], error="; ".join(notes) or None)

    candidates = _fuse(ranked_lists)

    # The reranker is a separate service from the vector tier: it can score
    # lexical-only candidate sets perfectly well, so it stays on even when
    # Qdrant/the embedder are disabled.
    reranked = await _maybe_rerank(
        query,
        candidates,
        top_k,
        rerank_fn or rerank,
        notes,
        enabled=settings.get_rerank_enabled(),
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


def _fuse(ranked_lists: list[list[Candidate]]) -> list[Candidate]:
    """Reciprocal-rank fusion over each tier's independent ranking.

    Candidates are fused by identity, not merged across tiers: a kiwix article
    and a curated chunk are different artifacts even when they discuss the same
    thing, and each carries its own citation.
    """
    scores: dict[int, float] = {}
    by_key: dict[int, Candidate] = {}
    for ranked in ranked_lists:
        for rank, candidate in enumerate(ranked, start=1):
            key = id(candidate)
            candidate._tier_rank = rank
            scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank)
            by_key[key] = candidate
    for key, candidate in by_key.items():
        candidate.fusion_score = scores[key]
    # Stable tie-break on tier rank so equal fusion scores keep a deterministic,
    # interleaved order rather than depending on dict/gather ordering.
    return sorted(
        by_key.values(), key=lambda c: (-(c.fusion_score or 0.0), c._tier_rank, c.title)
    )


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
            out = []
            for index, score in order:
                candidates[index].rerank_score = score
                out.append(candidates[index])
            return out
        except Exception as exc:  # noqa: BLE001 - fall back to fusion ordering
            notes.append(f"reranker unavailable ({type(exc).__name__})")
    # Fallback: the RRF order candidates already arrived in.
    return candidates
