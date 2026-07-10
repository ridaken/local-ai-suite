"""kb_read — read the full text of a knowledge-base article, paginated.

kb_search returns short snippets; this is the follow-up: the model passes a
result's source URL back and gets the article body in fixed-size character
windows, paging with `offset` until satisfied. Stateless — the offset travels
in the call, so no session bookkeeping in the gateway.

Only URLs on the configured kiwix-serve host are fetched. That restriction is
the whole security story: without it this tool would be a generic fetch the
model could point at anything reachable from the gateway container.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx

from .. import config

# Tags whose text content is never article prose.
_SKIP_TAGS = {"script", "style", "noscript", "head", "template"}
# Tags that imply a break in the text flow; rendered as newlines so the
# extracted text keeps paragraph structure instead of running together.
_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "caption", "dd",
    "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav",
    "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul",
}

_MULTI_BLANK_RE = re.compile(r"\n{3,}")
_LINE_WS_RE = re.compile(r"[ \t]+")


class _TextExtractor(HTMLParser):
    """Extract readable text from article HTML, preserving paragraph breaks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self.title = ""

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip_depth == 0 and data:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        lines = [_LINE_WS_RE.sub(" ", line).strip() for line in raw.split("\n")]
        return _MULTI_BLANK_RE.sub("\n\n", "\n".join(lines)).strip()


def extract_text(html: str) -> tuple[str, str]:
    """(title, body text) from article HTML."""
    parser = _TextExtractor()
    parser.feed(html)
    return parser.title.strip(), parser.text()


def content_url(source: str) -> str | None:
    """Map a kb_search citation URL to the kiwix content endpoint to fetch.

    Returns None unless the URL is on the configured KIWIX_URL host — kb_read
    must not be usable as a generic fetch tool. Handles both the viewer form
    (/viewer#<book>/<path>, what kiwix search results link to) and direct
    /content/<book>/<path> URLs.
    """
    kiwix = urlsplit(config.KIWIX_URL)
    parsed = urlsplit(source.strip())
    if parsed.scheme != kiwix.scheme or parsed.netloc != kiwix.netloc:
        return None
    if parsed.fragment and parsed.path.rstrip("/").endswith("viewer"):
        return f"{config.KIWIX_URL}/content/{parsed.fragment.lstrip('/')}"
    if parsed.path:
        # /content/... or an older direct article path; host is already
        # validated, so passing the path through as-is is safe.
        return f"{config.KIWIX_URL}{parsed.path}"
    return None


def window(text: str, offset: int, size: int) -> tuple[str, int, int, int]:
    """(body, start, end, total) for one page of `text`."""
    total = len(text)
    start = max(0, offset)
    end = min(start + size, total)
    return text[start:end], start, end, total


async def _fetch_html(url: str) -> httpx.Response:
    async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT, follow_redirects=True) as client:
        return await client.get(url, headers={"User-Agent": config.USER_AGENT})


async def kb_read(source: str, offset: int = 0) -> str:
    """Read a knowledge-base article in full, one window at a time."""
    url = content_url(source)
    if url is None:
        return (
            "kb_read error: source must be a knowledge-base URL from a kb_search "
            f"result (host {config.KIWIX_URL}). Got: {source}"
        )

    try:
        resp = await _fetch_html(url)
    except httpx.HTTPError as exc:
        return f"kb_read error: could not reach the knowledge base ({exc})."
    if resp.status_code == 404:
        return (
            f"kb_read error: article not found at {source}. Use a source URL "
            "exactly as returned by kb_search."
        )
    if resp.status_code != 200:
        return f"kb_read error: knowledge base returned HTTP {resp.status_code}."

    title, text = extract_text(resp.text)
    if not text:
        return f"kb_read: no readable text at {source}."

    body, start, end, total = window(text, offset, config.KB_READ_WINDOW_CHARS)
    if start >= total:
        return (
            f"kb_read: offset {offset} is past the end of this article "
            f"({total} characters total)."
        )

    lines = [f'"{title or source}" — characters {start}-{end} of {total}:', "", body, ""]
    if end < total:
        lines.append(
            f"[{total - end} characters remain — call kb_read with offset={end} to continue]"
        )
    else:
        lines.append("[end of article]")
    lines.append(f"source: {source}")
    return "\n".join(lines)
