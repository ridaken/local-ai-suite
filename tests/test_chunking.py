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
