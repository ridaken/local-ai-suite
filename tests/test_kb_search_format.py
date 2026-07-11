"""Tests for kb_search result formatting: snippet policy per tier + kb_read hint."""

from mcp_gateway.tools.kb_search import _format
from retrieval.hybrid import Candidate


def _kb(text: str) -> Candidate:
    return Candidate(
        title="Article", text=text, citation="http://kiwix:8080/viewer#b/A/X", source="kb"
    )


def _curated(text: str) -> Candidate:
    return Candidate(title="chunk", text=text, citation="repo/src/main.py", source="curated")


def test_lexical_snippets_are_trimmed_to_preview():
    from mcp_gateway.tools.kb_search import _KB_SNIPPET_CHARS

    out = _format("q", [_kb("x" * 2000)])
    assert "x" * (_KB_SNIPPET_CHARS - 3) + "..." in out
    assert "x" * (_KB_SNIPPET_CHARS + 1) not in out


def test_curated_chunks_pass_through_whole():
    # Regression: vector chunks are sized at ingest (CHUNK_MAX_CHARS); trimming
    # them again to 400 chars threw away most of what the vector tier retrieved.
    chunk = "y" * 2000
    out = _format("q", [_curated(chunk)])
    assert chunk in out


def test_kb_read_hint_present_only_with_lexical_results():
    with_kb = _format("q", [_kb("snippet"), _curated("chunk")])
    assert "kb_read" in with_kb

    curated_only = _format("q", [_curated("chunk")])
    assert "kb_read" not in curated_only
