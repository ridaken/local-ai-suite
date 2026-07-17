"""Tests for code-aware chunking."""

from ingest.chunking import Chunk, chunk_file, language_for
from mcp_gateway import config

PY = '''\
import os


def alpha(x):
    """doc"""
    return x + 1


class Widget:
    def method_a(self):
        return 1

    def method_b(self):
        return 2


TOP_LEVEL = 42
'''


def test_language_detection():
    assert language_for("a/b/foo.py") == "python"
    assert language_for("README.md") == "markdown"
    assert language_for("weird.unknownext") == "text"
    assert language_for("noext") == "text"


def test_python_chunks_by_symbol():
    chunks = chunk_file("pkg/mod.py", PY)
    symbols = {c.symbol for c in chunks}
    # small class stays whole; top-level function and module residue are separate
    assert "alpha" in symbols
    assert "Widget" in symbols
    assert "<module>" in symbols
    alpha = next(c for c in chunks if c.symbol == "alpha")
    assert "return x + 1" in alpha.text
    assert alpha.language == "python"


def test_chunk_ids_and_citation_are_stable():
    a = chunk_file("pkg/mod.py", PY)
    b = chunk_file("pkg/mod.py", PY)
    assert [c.chunk_id for c in a] == [c.chunk_id for c in b]
    alpha = next(c for c in a if c.symbol == "alpha")
    assert alpha.citation == f"pkg/mod.py:{alpha.start_line}"
    assert alpha.payload()["path"] == "pkg/mod.py"


def test_unparseable_python_falls_back_to_generic():
    chunks = chunk_file("broken.py", "def oops(:\n  pass\n")
    assert chunks and all(isinstance(c, Chunk) for c in chunks)


def test_generic_chunking_splits_long_text():
    text = "\n".join(f"line {i} " + "x" * 80 for i in range(200))
    chunks = chunk_file("notes.txt", text)
    assert len(chunks) > 1
    assert all(c.language == "text" for c in chunks)
    # line ranges are ordered and contiguous-ish (overlap allowed)
    assert chunks[0].start_line == 1
    assert chunks[-1].end_line == 200


def test_large_class_method_decorators_stay_with_method(monkeypatch):
    monkeypatch.setattr(config, "CHUNK_MAX_CHARS", 80)
    text = '''\
class Widget:
    kind = "demo"

    @classmethod
    def build(cls):
        return cls()

    def other(self):
        return 1
'''

    chunks = chunk_file("pkg/widget.py", text)
    header = next(c for c in chunks if c.symbol == "Widget (header)")
    method = next(c for c in chunks if c.symbol == "Widget.build")
    assert "@classmethod" not in header.text
    assert method.start_line == 4
    assert method.text.startswith("    @classmethod")


# --- Phase 3: hard size cap ---------------------------------------------------


def test_long_symbol_is_split_under_the_cap(monkeypatch):
    """Symbol chunking keeps a definition intact regardless of size, so a huge
    function would otherwise escape as one oversized chunk the embedder rejects."""
    monkeypatch.setattr(config, "CHUNK_MAX_CHARS", 300)
    body = "\n".join(f"    value_{i} = {i}" for i in range(200))
    chunks = chunk_file("big.py", f"def enormous():\n{body}\n")

    assert len(chunks) > 1
    assert all(len(c.text) <= 300 for c in chunks)
    assert all("enormous" in c.symbol for c in chunks)
    assert len({c.chunk_id for c in chunks}) == len(chunks)


def test_single_long_line_is_hard_split(monkeypatch):
    """A minified file has no line boundary to split on; the cap still holds."""
    monkeypatch.setattr(config, "CHUNK_MAX_CHARS", 256)
    chunks = chunk_file("bundle.js", "a" * 5000 + "\n")

    assert len(chunks) > 1
    assert all(len(c.text) <= 256 for c in chunks)
    assert "".join(c.text for c in chunks) == "a" * 5000


def test_generic_window_chunks_respect_the_cap(monkeypatch):
    monkeypatch.setattr(config, "CHUNK_MAX_CHARS", 400)
    monkeypatch.setattr(config, "CHUNK_OVERLAP_CHARS", 50)
    text = "\n".join(f"line {i} " + "z" * 100 for i in range(100))
    assert all(len(c.text) <= 400 for c in chunk_file("notes.txt", text))


# --- Phase 3: markdown chunking -----------------------------------------------

MD = """\
intro paragraph

# Install

run the installer

## Options

- verbose
- quiet

# Usage

call the thing
"""


def test_markdown_chunks_by_heading():
    chunks = chunk_file("README.md", MD)
    assert [c.symbol for c in chunks] == ["<preamble>", "Install", "Options", "Usage"]
    assert all(c.language == "markdown" for c in chunks)


def test_markdown_section_keeps_its_heading_with_its_body():
    chunks = {c.symbol: c.text for c in chunk_file("README.md", MD)}
    assert chunks["Install"].startswith("# Install")
    assert "run the installer" in chunks["Install"]


def test_markdown_ignores_headings_inside_code_fences():
    """A '#' comment in a shell block is not a heading — splitting there would
    cut the fence in half and strand the closing ```."""
    text = "# Real\n\n```sh\n# not a heading\necho hi\n```\n\n# Also Real\n\nbody\n"
    chunks = chunk_file("guide.md", text)
    assert [c.symbol for c in chunks] == ["Real", "Also Real"]
    assert "echo hi" in chunks[0].text
    assert chunks[0].text.count("```") == 2


def test_markdown_without_headings_falls_back_to_windows():
    chunks = chunk_file("plain.md", "just prose, no headings at all\n")
    assert [c.symbol for c in chunks] == ["lines 1-1"]


def test_markdown_line_ranges_are_reported():
    chunks = {c.symbol: (c.start_line, c.end_line) for c in chunk_file("README.md", MD)}
    assert chunks["Install"][0] == 3
    assert chunks["Usage"][0] == 12
