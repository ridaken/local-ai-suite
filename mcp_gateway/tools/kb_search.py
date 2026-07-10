"""kb_search — knowledge-base search.

Phase 2: hybrid retrieval. When the vector tier is enabled (Qdrant + embedder set)
this merges Kiwix full-text hits with vector hits over your curated corpora and
reranks them; otherwise it degrades to Kiwix-only lexical search (Phase 1). The
tool's return contract is unchanged — a cited, model-readable string.
"""

from __future__ import annotations

from retrieval.hybrid import Candidate, hybrid_search

from .. import config

# Lexical hits are trimmed to a preview — kb_read exists to pull the full
# article. Curated vector chunks are NOT trimmed: they were already sized at
# ingest (CHUNK_MAX_CHARS) and their file-path citations aren't kb_read-able,
# so the chunk itself is all the model will ever see of them.
_KB_SNIPPET_CHARS = 400


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
            "Snippets are short previews — call kb_read with a result's source "
            "URL to read the full article."
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
    return _format(query, result.candidates, result.warning)
