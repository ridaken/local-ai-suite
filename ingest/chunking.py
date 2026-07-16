"""Structure-aware chunking.

Naive fixed-size splitting mangles code and prose alike, so each language is cut
on its own natural boundaries: Python by symbol (function / method / class) via
the AST, keeping a definition intact with its decorators and docstring; Markdown
by heading, keeping a section with the heading that introduces it. Everything
else falls back to a line-aligned, overlapping window splitter. Every chunk
carries path / language / symbol / line-range metadata so retrieval can cite and
filter precisely.

Structural chunking ignores size by design, so chunk_file applies a hard
CHUNK_MAX_CHARS cap on the way out — the embedder truncates or rejects anything
larger, and silently embedding half a chunk is worse than splitting it.
"""

from __future__ import annotations

import ast
import re
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
    """Chunk one file into symbol-, heading-, or window-based chunks.

    Guarantees every returned chunk is within CHUNK_MAX_CHARS: the embedder
    truncates or rejects anything larger, so a single 5k-line function or a
    minified one-liner must not escape as one oversized chunk.
    """
    language = language_for(path)
    chunks: list[Chunk] | None = None
    if language == "python":
        chunks = _chunk_python(path, text)
    elif language == "markdown":
        chunks = _chunk_markdown(path, text)
    if chunks is None:
        chunks = _chunk_generic(path, text, language)
    return _enforce_max_chars(chunks)


def _enforce_max_chars(chunks: list[Chunk]) -> list[Chunk]:
    out: list[Chunk] = []
    for chunk in chunks:
        out.extend(_split_oversized(chunk))
    return out


def _split_oversized(chunk: Chunk) -> list[Chunk]:
    """Split one over-long chunk into line-aligned parts under the cap.

    Symbol and heading chunking deliberately ignore size to keep a definition
    intact, so this is the backstop. A single line longer than the cap (minified
    JS, a giant data literal) has no line boundary to split on and is cut at
    character boundaries — nothing else can bring it under.
    """
    max_chars = config.CHUNK_MAX_CHARS
    if len(chunk.text) <= max_chars:
        return [chunk]

    out: list[Chunk] = []
    buf: list[str] = []
    buf_len = 0
    start_line = chunk.start_line
    line_no = chunk.start_line
    part = 0

    def flush(end_line: int) -> None:
        nonlocal buf, buf_len, part
        body = "\n".join(buf).strip("\n")
        if body.strip():
            part += 1
            out.append(
                Chunk(
                    chunk.path,
                    chunk.language,
                    f"{chunk.symbol} (part {part})",
                    start_line,
                    max(start_line, end_line),
                    body,
                )
            )
        buf, buf_len = [], 0

    for line in chunk.text.split("\n"):
        segments = [line[i : i + max_chars] for i in range(0, len(line), max_chars)] or [""]
        for segment in segments:
            if buf and buf_len + len(segment) + 1 > max_chars:
                flush(line_no)
                start_line = line_no
            buf.append(segment)
            buf_len += len(segment) + 1
        line_no += 1
    flush(line_no - 1)
    return out


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
    header_end = (_def_start(methods[0]) - 1) if methods else end
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


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)")


def _chunk_markdown(path: str, text: str) -> list[Chunk] | None:
    """Chunk markdown at headings, keeping each section whole.

    Window splitting cuts prose mid-section and strands a heading from the text
    it introduces; a section is the natural retrieval unit. Fenced code is
    tracked so a `#` comment inside a shell/python block is not mistaken for a
    heading and used to split the fence in half.
    """
    lines = text.splitlines()
    if not lines:
        return []

    sections: list[tuple[str, int, int]] = []  # (symbol, start_line, end_line)
    fence: str | None = None
    symbol = "<preamble>"
    start = 1
    found_heading = False

    for i, line in enumerate(lines, start=1):
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)[0] * 3
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is not None:
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            found_heading = True
            if i > start:
                sections.append((symbol, start, i - 1))
            symbol = heading.group(2)
            start = i
    sections.append((symbol, start, len(lines)))

    # A file with no headings gains nothing here — let the window splitter,
    # which carries overlap between chunks, handle it instead.
    if not found_heading:
        return None

    return [
        Chunk(path, "markdown", symbol, s, e, _slice(lines, s, e))
        for symbol, s, e in sections
        if _slice(lines, s, e).strip()
    ]


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
            # Carry an overlap tail so context isn't cut mid-thought. The tail
            # must stay within the overlap budget: taking a line that overshoots
            # it re-emits that line as a whole extra chunk at the final flush,
            # which for a single over-long line (a minified bundle) duplicated
            # the entire file.
            tail, tail_len, kept = [], 0, 0
            for prev in reversed(buf):
                if tail_len + len(prev) + 1 > overlap:
                    break
                tail.insert(0, prev)
                tail_len += len(prev) + 1
                kept += 1
            buf = tail
            buf_len = tail_len
            start_line = i - kept + 1

    flush(len(lines))
    return chunks
