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
from urllib.parse import urljoin, urlsplit

import httpx

from retrieval.lexical import QUERY_FILLER

from .. import config
from ..limits import (
    ToolInputError,
    UpstreamResponseError,
    error_text,
    response_bytes,
    validate_offset,
    validate_source,
)

# Fallback window size when KB_READ_WINDOW_CHARS is set to a non-positive value.
_DEFAULT_WINDOW = 4000

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


def _on_kiwix_host(url: str) -> bool:
    """True only for URLs on the internal KIWIX_URL or the public KIWIX_PUBLIC_URL
    (scheme + host + port). Citations use the public host; the gateway itself and
    kiwix's own redirects use the internal one — both are legitimate."""
    parsed = urlsplit(url)
    allowed = {
        (urlsplit(base).scheme, urlsplit(base).netloc)
        for base in (config.KIWIX_URL, config.KIWIX_PUBLIC_URL)
    }
    return (parsed.scheme, parsed.netloc) in allowed


def content_url(source: str) -> str | None:
    """Map a kb_search citation URL to the internal kiwix content endpoint.

    Returns None unless the URL is on the kiwix host (internal or public) —
    kb_read must not be usable as a generic fetch tool. Always rewrites to the
    internal KIWIX_URL for the actual fetch, since a citation may use the public
    host (e.g. localhost:8080) that the gateway container can't resolve. Handles
    both the viewer form (/viewer#<book>/<path>) and direct /content/... URLs.
    """
    parsed = urlsplit(source.strip())
    if not _on_kiwix_host(source.strip()):
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


# Short/function words that shouldn't drive excerpt placement — otherwise a
# query like "symptoms of hypothyroidism" could center on the first "of". Also
# folds in the conversational-filler set (fun/facts/random/tell...) so that a
# query the model phrased as "Roman Empire fun facts" doesn't center the excerpt
# on a stray "fun facts" citation instead of the Roman Empire itself.
_EXCERPT_STOPWORDS = QUERY_FILLER | frozenset(
    "a an and are as at be by for from how in is of on or that the to what when "
    "where which who why with".split()
)
_WORD_RE = re.compile(r"[a-z0-9]+")


def _query_terms(query: str) -> list[str]:
    return [
        t for t in _WORD_RE.findall(query.lower()) if len(t) > 2 and t not in _EXCERPT_STOPWORDS
    ]


def best_excerpt(text: str, query: str, size: int) -> str:
    """A query-relevant window of `text`.

    kiwix's own search snippet centers on the densest match region, which for
    many queries is a shared navigation/infobox block (all thyroid articles
    embed the same disease navbox), yielding identical, useless snippets. This
    instead centers the window on the first place a meaningful query term
    actually appears in the article body, falling back to the lead.
    """
    if not text:
        return ""
    lowered = text.lower()
    # Center on the most *discriminating* query term — the one with the fewest
    # occurrences — not the first. A term that is also the article title (e.g.
    # "hypothyroidism") matches at position 0 and everywhere, which would pin the
    # excerpt to the lead/infobox; the rarer term ("symptoms") marks the section
    # the query is actually about.
    present = [(lowered.count(t), lowered.find(t)) for t in _query_terms(query)]
    present = [(count, pos) for count, pos in present if pos >= 0]
    idx = min(present)[1] if present else -1
    if idx < 0:
        excerpt = text[:size].strip()
        return excerpt + ("…" if len(text) > size else "")
    # Back up a little so the term isn't the very first word, snapping to a
    # word boundary, then take a window forward.
    start = max(0, idx - 60)
    if start > 0:
        space = text.find(" ", start)
        start = space + 1 if 0 <= space < idx else start
    end = min(len(text), start + size)
    excerpt = text[start:end].strip()
    return ("…" if start > 0 else "") + excerpt + ("…" if end < len(text) else "")


async def article_excerpt(source: str, query: str, size: int) -> str | None:
    """Fetch a kb_search hit's article and return a query-relevant excerpt, or
    None if it can't be fetched (caller falls back to the kiwix snippet)."""
    url = content_url(source)
    if url is None:
        return None
    try:
        resp = await _fetch_article(url)
    except (httpx.HTTPError, ForeignRedirectError):
        return None
    if resp.status_code != 200:
        return None
    try:
        body = response_bytes(resp).decode(resp.encoding or "utf-8", errors="replace")
    except UpstreamResponseError:
        return None
    _title, text = extract_text(body)
    return best_excerpt(text, query, size) or None


class ForeignRedirectError(Exception):
    """The kiwix host tried to redirect kb_read off the kiwix host."""


_REDIRECT_CODES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 5


async def _fetch_html(url: str) -> httpx.Response:
    """One request, no automatic redirects — redirect policy lives in
    _fetch_article so every hop is host-validated."""
    async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT, follow_redirects=False) as client:
        return await client.get(url, headers={"User-Agent": config.USER_AGENT})


async def _fetch_article(url: str) -> httpx.Response:
    """Fetch with manual redirect following, re-validating the host at every
    hop. httpx's automatic following would happily leave the kiwix host on a
    Location header, silently defeating content_url's validation."""
    for _ in range(_MAX_REDIRECTS + 1):
        resp = await _fetch_html(url)
        if resp.status_code not in _REDIRECT_CODES:
            return resp
        target = urljoin(url, resp.headers.get("location", ""))
        if not _on_kiwix_host(target):
            raise ForeignRedirectError(target)
        url = target
    raise httpx.TooManyRedirects(f"more than {_MAX_REDIRECTS} redirects", request=resp.request)


async def kb_read(source: str, offset: int = 0) -> str:
    """Read a knowledge-base article in full, one window at a time."""
    try:
        source = validate_source(source)
        offset = validate_offset(offset)
    except ToolInputError as exc:
        return error_text("kb_read", exc)
    url = content_url(source)
    if url is None:
        return (
            "kb_read error: source must be a knowledge-base URL from a kb_search "
            f"result (host {config.KIWIX_URL}). Got: {source}"
        )

    try:
        resp = await _fetch_article(url)
    except ForeignRedirectError as exc:
        return (
            "kb_read error: the knowledge base redirected outside its own host "
            f"({exc}) — refusing to follow."
        )
    except httpx.HTTPError:
        return "kb_read error [upstream_unavailable]: knowledge-base request failed."
    if resp.status_code == 404:
        return (
            f"kb_read error: article not found at {source}. Use a source URL "
            "exactly as returned by kb_search."
        )
    if resp.status_code != 200:
        return f"kb_read error [upstream_http]: knowledge base returned HTTP {resp.status_code}."

    try:
        body = response_bytes(resp).decode(resp.encoding or "utf-8", errors="replace")
    except UpstreamResponseError as exc:
        return error_text("kb_read", exc)
    title, text = extract_text(body)
    if not text:
        return f"kb_read: no readable text at {source}."

    # A non-positive window would page 0 chars and tell the model to retry at
    # the same offset forever; treat it as misconfiguration and use the default.
    configured_size = config.KB_READ_WINDOW_CHARS
    size = min(16000, max(500, configured_size if configured_size > 0 else _DEFAULT_WINDOW))
    body, start, end, total = window(text, offset, size)
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
