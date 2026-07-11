"""kb_search — knowledge-base search.

Phase 2: hybrid retrieval. When the vector tier is enabled (Qdrant + embedder set)
this merges Kiwix full-text hits with vector hits over your curated corpora and
reranks them; otherwise it degrades to Kiwix-only lexical search (Phase 1). The
tool's return contract is unchanged — a cited, model-readable string.
"""

from __future__ import annotations

import asyncio

from retrieval.hybrid import Candidate, hybrid_search

from .. import config
from .kb_read import article_excerpt

# Kiwix hits are shown as a preview — kb_read pulls the full article. Curated
# vector chunks are NOT trimmed: they were already sized at ingest
# (CHUNK_MAX_CHARS) and their file-path citations aren't kb_read-able, so the
# chunk itself is all the model will ever see of them.
_KB_SNIPPET_CHARS = 600
# Target length of the fetched query-relevant excerpt (kept under the display
# cap above so enriched excerpts pass through _format untrimmed).
_KB_EXCERPT_CHARS = 500


async def _enrich_kb_excerpts(query: str, candidates: list[Candidate]) -> None:
    """Replace kiwix's match-snippet with a fetched, query-relevant excerpt for
    each kiwix hit. Best-effort: any hit whose article can't be fetched keeps
    its original snippet."""
    kb = [c for c in candidates if c.source == "kb" and c.citation]
    if not kb:
        return
    excerpts = await asyncio.gather(
        *(article_excerpt(c.citation, query, _KB_EXCERPT_CHARS) for c in kb),
        return_exceptions=True,
    )
    for candidate, excerpt in zip(kb, excerpts, strict=True):
        if isinstance(excerpt, str) and excerpt:
            candidate.text = excerpt


def _format(query: str, candidates: list[Candidate], warning: str | None = None) -> str:
    lines = [f'Knowledge base results for "{query}":', ""]
    if warning:
        lines += [f"Warning: partial results ({warning}).", ""]
    for i, c in enumerate(candidates, start=1):
        snippet = c.text
        if c.source == "kb" and len(snippet) > _KB_SNIPPET_CHARS:
            snippet = snippet[: _KB_SNIPPET_CHARS - 3] + "..."
        lines.append(f"{i}. [{c.source}] {c.title}")
        if snippet:
            lines.append(f"   {snippet}")
        if c.citation:
            lines.append(f"   source: {c.citation}")
        lines.append("")
    lines.append("Cite the source paths / URLs above.")
    if any(c.source == "kb" for c in candidates):
        lines.append(
            "These are excerpts, not full articles. If the answer isn't fully "
            "contained above, call kb_read with the most relevant source URL to "
            "read the full article before answering — do not fall back to prior "
            "knowledge when a relevant source is listed."
        )
    return "\n".join(lines).rstrip()


async def kb_search(query: str, limit: int | None = None) -> str:
    """Search the knowledge base (offline docs + your curated corpora) and return
    cited passages."""
    limit = limit or config.KB_SEARCH_LIMIT
    result = await hybrid_search(query, limit)
    if not result.candidates:
        if result.error:
            return (
                f"kb_search: no results ({result.error}). Check that kiwix-serve is "
                f"running with a ZIM loaded, and (for the vector tier) that Qdrant and "
                f"the embedder are reachable."
            )
        return f'No knowledge-base results for "{query}".'
    if config.KB_SEARCH_FETCH_EXCERPTS:
        await _enrich_kb_excerpts(query, result.candidates)
    return _format(query, result.candidates, result.warning)
