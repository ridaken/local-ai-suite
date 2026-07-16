"""kb_search — knowledge-base search.

Hybrid retrieval: when the vector tier is enabled (Qdrant + embedder set) this
fuses Kiwix full-text hits with vector hits over your curated corpora and reranks
them; otherwise it degrades to Kiwix-only lexical search. Returns a typed
SearchResponse — the server renders both structured content and a text fallback.
"""

from __future__ import annotations

import asyncio

import httpx

from retrieval.hybrid import Candidate, hybrid_search

from .. import config
from ..limits import ToolInputError, clamp_limit, validate_query
from ..schemas import SearchResponse, SearchResult, render_search, search_error
from .kb_read import article_excerpt

# Kiwix hits are shown as a preview — kb_read pulls the full article. Curated
# vector chunks are NOT trimmed: they were already sized at ingest
# (CHUNK_MAX_CHARS) and their file-path citations aren't kb_read-able, so the
# chunk itself is all the model will ever see of them.
_KB_SNIPPET_CHARS = 600
# Target length of the fetched query-relevant excerpt (kept under the display
# cap above so enriched excerpts pass through untrimmed).
_KB_EXCERPT_CHARS = 500

_KB_FOOTER = (
    "These are excerpts, not full articles. If the answer isn't fully contained "
    "above, call kb_read with the most relevant source URL to read the full "
    "article before answering — do not fall back to prior knowledge when a "
    "relevant source is listed."
)


async def _enrich_kb_excerpts(query: str, candidates: list[Candidate]) -> None:
    """Replace kiwix's match-snippet with a fetched, query-relevant excerpt for
    each kiwix hit. Best-effort: any hit whose article can't be fetched keeps
    its original snippet.

    The fan-out is bounded and shares one connection pool: unbounded gather over
    every hit would open a connection per result and let one slow search saturate
    kiwix-serve.
    """
    kb = [c for c in candidates if c.source == "kb" and c.citation]
    if not kb:
        return
    semaphore = asyncio.Semaphore(config.KB_EXCERPT_CONCURRENCY)

    async def fetch(candidate: Candidate, client: httpx.AsyncClient) -> str | None:
        async with semaphore:
            return await article_excerpt(
                candidate.citation, query, _KB_EXCERPT_CHARS, client=client
            )

    async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT, follow_redirects=False) as client:
        excerpts = await asyncio.gather(*(fetch(c, client) for c in kb), return_exceptions=True)
    for candidate, excerpt in zip(kb, excerpts, strict=True):
        if isinstance(excerpt, str) and excerpt:
            candidate.text = excerpt


def _excerpt(candidate: Candidate) -> str:
    text = candidate.text
    if candidate.source == "kb" and len(text) > _KB_SNIPPET_CHARS:
        return text[: _KB_SNIPPET_CHARS - 3] + "..."
    return text


def _to_result(candidate: Candidate) -> SearchResult:
    return SearchResult(
        id=candidate.id or candidate.citation,
        title=candidate.title,
        excerpt=_excerpt(candidate),
        source_kind=candidate.source,
        citation=candidate.citation,
        corpus_version=candidate.corpus_version,
        retrieval_score=candidate.score,
        rerank_score=candidate.rerank_score,
    )


async def kb_search_response(query: str, limit: int | None = None) -> SearchResponse:
    """Search the knowledge base (offline docs + your curated corpora)."""
    try:
        query = validate_query(query)
        limit = clamp_limit(limit, config.KB_SEARCH_LIMIT)
    except ToolInputError as exc:
        return search_error(str(query), exc.code, f"kb_search error [{exc.code}]: {exc}")

    result = await hybrid_search(query, limit)
    if not result.candidates and result.error:
        return search_error(
            query,
            "retrieval_unavailable",
            f"kb_search: no results ({result.error}). Check that kiwix-serve is "
            f"running with a ZIM loaded, and (for the vector tier) that Qdrant and "
            f"the embedder are reachable.",
        )
    if config.KB_SEARCH_FETCH_EXCERPTS:
        await _enrich_kb_excerpts(query, result.candidates)
    return SearchResponse(
        query=query,
        results=[_to_result(c) for c in result.candidates],
        warnings=[result.warning] if result.warning else [],
    )


def render(response: SearchResponse) -> str:
    footer = _KB_FOOTER if any(r.source_kind == "kb" for r in response.results) else ""
    return render_search(response, heading="Knowledge base results", footer=footer)


async def kb_search(query: str, limit: int | None = None) -> str:
    """Text-only entry point (stdio clients and tests)."""
    return render(await kb_search_response(query, limit))
