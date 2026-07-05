"""Code-aware chunking.

Naive fixed-size splitting mangles code, so Python files are chunked by symbol
(function / method / class) using the AST, keeping each definition intact with its
decorators and docstring. Everything else falls back to a line-aligned, overlapping
window splitter. Every chunk carries path / language / symbol / line-range metadata
so retrieval can cite and filter precisely.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from mcp_gateway import config

_LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".md": "markdown",
    ".rs": "rust", ".go": "go", ".java": "java", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".hpp": "cpp", ".rb": "ruby", ".sh": "shell",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".json": "json",
    ".html": "html", ".css": "css", ".txt": "text", ".sql": "sql",
}


@dataclass
class Chunk:
    path: str
    language: str
    symbol: str
    start_line: int
    end_line: int
    text: str
    chunk_id: str = field(default="")

    def __post_init__(self) -> None:
        if not self.chunk_id:
            self.chunk_id = f"{self.path}::{self.symbol}#{self.start_line}-{self.end_line}"

    @property
    def citation(self) -> str:
        return f"{self.path}:{self.start_line}"

    def payload(self) -> dict:
        return {
            "path": self.path,
            "language": self.language,
            "symbol": self.symbol,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "citation": self.citation,
            "text": self.text,
        }


def language_for(path: str) -> str:
    dot = path.rfind(".")
    ext = path[dot:].lower() if dot != -1 else ""
    return _LANG_BY_EXT.get(ext, "text")


def chunk_file(path: str, text: str) -> list[Chunk]:
    """Chunk one file into symbol- or window-based chunks."""
    language = language_for(path)
    if language == "python":
        chunks = _chunk_python(path, text)
        if chunks is not None:
            return chunks
    return _chunk_generic(path, text, language)


def _slice(lines: list[str], start: int, end: int) -> str:
    return "\n".join(lines[start - 1 : end]).strip("\n")


def _def_start(node: ast.AST) -> int:
    decorators = getattr(node, "decorator_list", [])
    starts = [node.lineno] + [d.lineno for d in decorators]
    return min(starts)


def _chunk_python(path: str, text: str) -> list[Chunk] | None:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None  # let the generic splitter handle unparseable python

    lines = text.splitlines()
    chunks: list[Chunk] = []
    covered_module_body = []  # top-level non-def statements gathered into a module chunk

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            chunks += _chunk_class(path, lines, node, text)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start, end = _def_start(node), node.end_lineno or node.lineno
            chunks.append(
                Chunk(path, "python", node.name, start, end, _slice(lines, start, end))
            )
        else:
            covered_module_body.append(node)

    module_text = "\n".join(
        _slice(lines, n.lineno, n.end_lineno or n.lineno) for n in covered_module_body
    ).strip()
    if module_text:
        first = covered_module_body[0].lineno
        last = covered_module_body[-1].end_lineno or covered_module_body[-1].lineno
        chunks.append(Chunk(path, "python", "<module>", first, last, module_text))

    return chunks or _chunk_generic(path, text, "python")


def _chunk_class(path: str, lines: list[str], node: ast.ClassDef, text: str) -> list[Chunk]:
    start, end = _def_start(node), node.end_lineno or node.lineno
    whole = _slice(lines, start, end)
    if len(whole) <= config.CHUNK_MAX_CHARS:
        return [Chunk(path, "python", node.name, start, end, whole)]

    # Large class: one chunk per method, plus a header chunk for the class body top.
    chunks: list[Chunk] = []
    methods = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    header_end = (methods[0].lineno - 1) if methods else end
    header = _slice(lines, start, header_end)
    if header.strip():
        chunks.append(Chunk(path, "python", f"{node.name} (header)", start, header_end, header))
    for m in methods:
        m_start, m_end = _def_start(m), m.end_lineno or m.lineno
        chunks.append(
            Chunk(
                path, "python", f"{node.name}.{m.name}", m_start, m_end,
                _slice(lines, m_start, m_end),
            )
        )
    return chunks


def _chunk_generic(path: str, text: str, language: str) -> list[Chunk]:
    lines = text.splitlines()
    if not lines:
        return []
    max_chars = config.CHUNK_MAX_CHARS
    overlap = config.CHUNK_OVERLAP_CHARS

    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_len = 0
    start_line = 1

    def flush(end_line: int) -> None:
        nonlocal buf, buf_len, start_line
        body = "\n".join(buf).strip("\n")
        if body.strip():
            sym = f"lines {start_line}-{end_line}"
            chunks.append(Chunk(path, language, sym, start_line, end_line, body))

    for i, line in enumerate(lines, start=1):
        buf.append(line)
        buf_len += len(line) + 1
        if buf_len >= max_chars:
            flush(i)
            # carry an overlap tail so context isn't cut mid-thought
            tail, tail_len, kept = [], 0, 0
            for prev in reversed(buf):
                if tail_len >= overlap:
                    break
                tail.insert(0, prev)
                tail_len += len(prev) + 1
                kept += 1
            buf = tail
            buf_len = tail_len
            start_line = i - kept + 1

    flush(len(lines))
    return chunks
