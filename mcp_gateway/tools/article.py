"""Read and search the contents of selected PubMed/PMC and arXiv articles."""

from __future__ import annotations

import asyncio
import io
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET

import httpx

from retrieval.rerank import rerank

from .. import config
from ..limits import (
    ToolInputError,
    UpstreamResponseError,
    clamp_limit,
    error_text,
    response_bytes,
    validate_offset,
    validate_query,
)
from ..schemas import (
    ArticlePassage,
    ArticlePassageResponse,
    ArticleReadResponse,
    ToolError,
    render_article_passages,
    render_article_read,
)
from ..settings_store import default_store
from .arxiv import fetch_arxiv_record
from .pubmed import fetch_pubmed_records, ncbi_get

_PUBMED_ID = re.compile(r"^pubmed:(\d{1,12})$")
_ARXIV_ID = re.compile(
    r"^arxiv:(\d{4}\.\d{4,5}(?:v\d+)?|[A-Za-z0-9._-]+/\d{7}(?:v\d+)?)$"
)
_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and are as at be by for from how in is it of on or that the this to was were what "
    "when where which who why with".split()
)
_ALLOWED_ARXIV_HOSTS = {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}
_REDIRECTS = {301, 302, 303, 307, 308}


@dataclass
class LoadedArticle:
    article_id: str
    provider: str
    title: str
    citation: str
    content_level: str
    extraction_method: str
    text: str
    license: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Chunk:
    section: str
    text: str
    offset: int
    end_offset: int
    lexical_score: float


_cache: OrderedDict[str, tuple[float, LoadedArticle, int]] = OrderedDict()
_cache_bytes = 0
_arxiv_locks: dict[int, asyncio.Lock] = {}


def _clean(value: str) -> str:
    return " ".join(value.split())


def _local(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _node_text(node: ET.Element | None) -> str:
    return _clean("".join(node.itertext())) if node is not None else ""


def parse_article_id(article_id: str) -> tuple[str, str]:
    if not isinstance(article_id, str):
        raise ToolInputError("invalid_article_id", "article_id must be a string")
    value = article_id.strip()
    pubmed = _PUBMED_ID.fullmatch(value)
    if pubmed:
        return "pubmed", pubmed.group(1)
    arxiv = _ARXIV_ID.fullmatch(value)
    if arxiv:
        return "arxiv", arxiv.group(1)
    raise ToolInputError(
        "invalid_article_id",
        "article_id must be a pubmed:<PMID> or arxiv:<arXiv-id> value returned by search",
    )


def _cache_get(article_id: str) -> LoadedArticle | None:
    item = _cache.get(article_id)
    if item is None:
        return None
    created, article, _size = item
    if time.monotonic() - created > config.ARTICLE_CACHE_TTL_SECONDS:
        _cache_remove(article_id)
        return None
    _cache.move_to_end(article_id)
    return article


def _cache_remove(article_id: str) -> None:
    global _cache_bytes
    item = _cache.pop(article_id, None)
    if item:
        _cache_bytes -= item[2]


def _cache_put(article: LoadedArticle) -> None:
    global _cache_bytes
    _cache_remove(article.article_id)
    size = len(article.text.encode("utf-8"))
    if size > config.ARTICLE_CACHE_MAX_BYTES:
        return
    _cache[article.article_id] = (time.monotonic(), article, size)
    _cache_bytes += size
    while (
        len(_cache) > config.ARTICLE_CACHE_MAX_ITEMS
        or _cache_bytes > config.ARTICLE_CACHE_MAX_BYTES
    ):
        key = next(iter(_cache))
        _cache_remove(key)


def parse_pmc_jats(payload: bytes) -> tuple[str, str, str | None]:
    """Return title, readable markdown-like article text, and license text."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise UpstreamResponseError("upstream_malformed", "PMC returned malformed XML") from exc

    title = ""
    for node in root.iter():
        if _local(node.tag) == "article-title":
            title = _node_text(node)
            if title:
                break
    license_text = None
    for node in root.iter():
        if _local(node.tag) in {"license", "permissions"}:
            candidate = _node_text(node)
            if candidate:
                license_text = candidate[:2000]
                break

    parts: list[str] = []
    abstract = next((node for node in root.iter() if _local(node.tag) == "abstract"), None)
    if abstract is not None and _node_text(abstract):
        parts += ["## Abstract", _node_text(abstract)]

    body = next((node for node in root.iter() if _local(node.tag) == "body"), None)

    def walk(node: ET.Element, depth: int = 2) -> None:
        tag = _local(node.tag)
        if tag == "sec":
            heading = next(
                (_node_text(child) for child in node if _local(child.tag) == "title"), ""
            )
            if heading:
                parts.append(f"{'#' * min(depth, 6)} {heading}")
            for child in node:
                if _local(child.tag) != "title":
                    walk(child, depth + 1)
            return
        if tag == "p":
            text = _node_text(node)
            if text:
                parts.append(text)
            return
        if tag == "list-item":
            text = _node_text(node)
            if text:
                parts.append(f"- {text}")
            return
        if tag == "table-wrap":
            label = next((_node_text(x) for x in node if _local(x.tag) == "label"), "Table")
            caption = next((_node_text(x) for x in node if _local(x.tag) == "caption"), "")
            parts.append(f"### {_clean(' '.join(x for x in [label, caption] if x))}")
            for row in (x for x in node.iter() if _local(x.tag) == "tr"):
                cells = [_node_text(x) for x in row if _local(x.tag) in {"td", "th"}]
                if any(cells):
                    parts.append(" | ".join(cells))
            return
        if tag == "fig":
            label = next((_node_text(x) for x in node if _local(x.tag) == "label"), "Figure")
            caption = next((_node_text(x) for x in node if _local(x.tag) == "caption"), "")
            if caption:
                parts.append(f"### {label}: {caption}")
            return
        for child in node:
            walk(child, depth)

    if body is not None:
        walk(body)

    back = next((node for node in root.iter() if _local(node.tag) == "back"), None)
    if back is not None:
        references = [
            _node_text(node)
            for node in back.iter()
            if _local(node.tag) in {"ref", "mixed-citation"}
        ]
        references = [value for value in references if value]
        if references:
            parts.append("## References")
            parts.extend(f"- {value}" for value in dict.fromkeys(references))

    text = "\n\n".join(part.strip() for part in parts if part.strip())
    return title, text, license_text


class _ArxivHTMLParser(HTMLParser):
    _skip = {
        "aside", "button", "dialog", "footer", "form", "header", "nav", "noscript",
        "script", "style", "svg", "template",
    }
    _blocks = {
        "article", "blockquote", "br", "caption", "dd", "div", "dl", "dt", "figcaption",
        "figure", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ol", "p", "pre",
        "section", "table", "td", "th", "tr", "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0
        self.article_depth = 0
        self.title = ""

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        attributes = dict(attrs)
        if tag == "meta" and attributes.get("name") == "citation_title":
            self.title = _clean(attributes.get("content", ""))
        if tag == "article":
            self.article_depth += 1
            self.parts.append("\n")
            return
        if not self.article_depth:
            return
        if tag in self._skip:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in self._blocks:
            self.parts.append("\n")
        if tag == "math":
            alt = dict(attrs).get("alttext")
            if alt:
                self.parts.append(f" {alt} ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "article":
            self.parts.append("\n")
            self.article_depth = max(0, self.article_depth - 1)
        elif not self.article_depth:
            return
        elif tag in self._skip:
            self.skip_depth = max(0, self.skip_depth - 1)
        elif not self.skip_depth and tag in self._blocks:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.article_depth and not self.skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        lines = [_clean(line) for line in "".join(self.parts).splitlines()]
        return "\n\n".join(line for line in lines if line)


def extract_arxiv_html(payload: bytes, encoding: str = "utf-8") -> tuple[str, str]:
    parser = _ArxivHTMLParser()
    parser.feed(payload.decode(encoding, errors="replace"))
    text = parser.text()
    title = parser.title or next((line for line in text.splitlines() if line.strip()), "")
    return title, text


def parse_arxiv_html(payload: bytes, encoding: str = "utf-8") -> str:
    return extract_arxiv_html(payload, encoding)[1]


def parse_pdf(payload: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(payload))
        if len(reader.pages) > 500:
            raise UpstreamResponseError("article_too_large", "PDF exceeds 500 pages")
        pages = [page.extract_text() or "" for page in reader.pages]
    except UpstreamResponseError:
        raise
    except Exception as exc:  # pypdf raises several format/encryption-specific errors
        raise UpstreamResponseError("pdf_extraction_failed", "could not extract arXiv PDF") from exc
    return "\n\n".join(page.strip() for page in pages if page.strip())


async def _fetch_arxiv_url(client: httpx.AsyncClient, url: str) -> httpx.Response:
    for _ in range(4):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_ARXIV_HOSTS:
            raise ToolInputError("foreign_redirect", "arXiv redirected to an unapproved host")
        response = await client.get(
            url, headers={"User-Agent": config.USER_AGENT}, follow_redirects=False
        )
        if response.status_code not in _REDIRECTS:
            return response
        url = urljoin(url, response.headers.get("location", ""))
    raise UpstreamResponseError("too_many_redirects", "arXiv returned too many redirects")


async def _load_pubmed(article_id: str, pmid: str) -> LoadedArticle:
    async with httpx.AsyncClient(timeout=max(config.HTTP_TIMEOUT, 60.0)) as client:
        records = await fetch_pubmed_records(client, [pmid])
        if not records:
            raise UpstreamResponseError("not_found", f"PubMed did not return PMID {pmid}")
        record = records[0]
        citation = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        warnings: list[str] = []
        if record.pmcid:
            try:
                response = await ncbi_get(
                    client,
                    "efetch.fcgi",
                    {"db": "pmc", "id": record.pmcid, "retmode": "xml"},
                )
                payload = response_bytes(response, maximum=config.ARTICLE_DOWNLOAD_MAX_BYTES)
                title, text, license_text = parse_pmc_jats(payload)
                if text:
                    return LoadedArticle(
                        article_id=article_id,
                        provider="pubmed",
                        title=title or record.title,
                        citation=citation,
                        content_level="full_text",
                        extraction_method="pmc_jats_xml",
                        text=text,
                        license=license_text,
                    )
                warnings.append("PMC returned no readable article body; using the abstract")
            except (httpx.HTTPError, UpstreamResponseError) as exc:
                warnings.append(
                    f"PMC full text unavailable ({type(exc).__name__}); using the abstract"
                )
        else:
            warnings.append("No PMCID is linked to this record; using the PubMed abstract")
        if record.abstract:
            return LoadedArticle(
                article_id=article_id,
                provider="pubmed",
                title=record.title,
                citation=citation,
                content_level="abstract",
                extraction_method="pubmed_xml",
                text=f"# {record.title}\n\n## Abstract\n\n{record.abstract}",
                warnings=warnings,
            )
        raise UpstreamResponseError(
            "content_unavailable",
            "The PubMed record has neither retrievable full text nor an abstract",
        )


async def _load_arxiv(article_id: str, arxiv_id: str) -> LoadedArticle:
    loop = asyncio.get_running_loop()
    lock = _arxiv_locks.setdefault(id(loop), asyncio.Lock())
    async with lock, httpx.AsyncClient(
        timeout=max(config.HTTP_TIMEOUT, 60.0), follow_redirects=True
    ) as client:
        metadata_warning = None
        try:
            record = await fetch_arxiv_record(client, arxiv_id)
        except (httpx.HTTPError, UpstreamResponseError) as exc:
            record = None
            metadata_warning = f"arXiv metadata unavailable ({type(exc).__name__})"
        title = record.title if record else arxiv_id
        abstract = record.abstract if record else ""
        citation = record.citation if record else f"https://arxiv.org/abs/{arxiv_id}"
        warnings: list[str] = [metadata_warning] if metadata_warning else []

        try:
            html_response = await _fetch_arxiv_url(client, f"https://arxiv.org/html/{arxiv_id}")
            if html_response.status_code == 200:
                payload = response_bytes(html_response, maximum=config.ARTICLE_DOWNLOAD_MAX_BYTES)
                html_title, text = extract_arxiv_html(
                    payload, html_response.encoding or "utf-8"
                )
                if text:
                    return LoadedArticle(
                        article_id=article_id,
                        provider="arxiv",
                        title=record.title if record else (html_title or title),
                        citation=citation,
                        content_level="full_text",
                        extraction_method="arxiv_html",
                        text=text,
                        warnings=warnings,
                    )
                warnings.append("arXiv HTML contained no readable text")
            elif html_response.status_code not in {404, 406}:
                warnings.append(f"arXiv HTML returned HTTP {html_response.status_code}")
        except (httpx.HTTPError, ToolInputError, UpstreamResponseError) as exc:
            warnings.append(f"arXiv HTML unavailable ({type(exc).__name__})")

        try:
            pdf_response = await _fetch_arxiv_url(client, f"https://arxiv.org/pdf/{arxiv_id}")
            if pdf_response.status_code == 200:
                payload = response_bytes(pdf_response, maximum=config.ARTICLE_DOWNLOAD_MAX_BYTES)
                text = parse_pdf(payload)
                if text:
                    warnings.append(
                        "PDF extraction may lose equations, columns, and table structure"
                    )
                    return LoadedArticle(
                        article_id=article_id,
                        provider="arxiv",
                        title=title,
                        citation=citation,
                        content_level="full_text",
                        extraction_method="arxiv_pdf_text",
                        text=text,
                        warnings=warnings,
                    )
            else:
                warnings.append(f"arXiv PDF returned HTTP {pdf_response.status_code}")
        except (httpx.HTTPError, ToolInputError, UpstreamResponseError) as exc:
            warnings.append(f"arXiv PDF unavailable ({type(exc).__name__})")

        if abstract:
            warnings.append("Full text unavailable; using the arXiv abstract")
            return LoadedArticle(
                article_id=article_id,
                provider="arxiv",
                title=title,
                citation=citation,
                content_level="abstract",
                extraction_method="arxiv_atom",
                text=f"# {title}\n\n## Abstract\n\n{abstract}",
                warnings=warnings,
            )
        raise UpstreamResponseError("content_unavailable", "arXiv returned no readable content")


async def load_article(article_id: str) -> LoadedArticle:
    provider, identifier = parse_article_id(article_id)
    canonical_id = f"{provider}:{identifier}"
    cached = _cache_get(canonical_id)
    if cached is not None:
        return cached
    article = (
        await _load_pubmed(canonical_id, identifier)
        if provider == "pubmed"
        else await _load_arxiv(canonical_id, identifier)
    )
    _cache_put(article)
    return article


def _section_at(text: str, offset: int) -> str:
    section = "Article"
    for match in re.finditer(r"(?m)^#{1,6}\s+(.+)$", text[:offset]):
        section = match.group(1).strip()
    return section


def _chunks(text: str, query: str) -> list[_Chunk]:
    terms = [term for term in _WORD.findall(query.lower()) if term not in _STOPWORDS]
    reference_heading = re.search(
        r"(?mi)^#{1,6}\s+(references|bibliography|literature cited)\s*$", text
    )
    searchable_end = reference_heading.start() if reference_heading else len(text)
    lowered = text[:searchable_end].lower()
    size, overlap = 2000, 200
    chunks = []
    start = 0
    while start < searchable_end:
        target = min(searchable_end, start + size)
        end = target
        if target < searchable_end:
            boundary = text.rfind("\n", start + size // 2, target)
            if boundary > start:
                end = boundary
        body = text[start:end].strip()
        if body:
            section = _section_at(text, start)
            opening_heading = re.match(r"^#{1,6}\s+(.+)$", body.splitlines()[0])
            if opening_heading:
                section = opening_heading.group(1).strip()
            low = lowered[start:end]
            phrase = low.count(query.lower()) * 3 if query else 0
            section_low = section.lower()
            section_matches = sum(section_low.count(term) for term in terms)
            evidence_heading = any(
                marker in section_low
                for marker in (
                    "abstract",
                    "conclusion",
                    "discussion",
                    "method",
                    "recommendation",
                    "result",
                    "rationale",
                )
            )
            score = float(
                phrase
                + sum(low.count(term) for term in terms)
                + section_matches * 3
                + (1 if evidence_heading else 0)
            )
            chunks.append(_Chunk(section, body, start, end, score))
        if end >= searchable_end:
            break
        start = max(start + 1, end - overlap)
    return chunks


async def article_find_response(
    article_id: str, query: str, limit: int = 5
) -> ArticlePassageResponse:
    try:
        query = validate_query(query)
        limit = clamp_limit(limit, 5)
        article = await load_article(article_id)
    except ToolInputError as exc:
        return ArticlePassageResponse(
            article_id=str(article_id),
            query=str(query),
            error=ToolError(code=exc.code, message=error_text("article_find", exc)),
        )
    except (httpx.HTTPError, UpstreamResponseError) as exc:
        code = getattr(exc, "code", "upstream_unavailable")
        return ArticlePassageResponse(
            article_id=str(article_id),
            query=str(query),
            error=ToolError(code=code, message=f"article_find error [{code}]: {exc}"),
        )

    candidates = sorted(
        _chunks(article.text, query), key=lambda chunk: chunk.lexical_score, reverse=True
    )[:40]
    selected = candidates[:limit]
    warnings = list(article.warnings)
    try:
        rerank_enabled = bool(config.RERANK_URL) and default_store().get_rerank_enabled()
    except Exception:
        rerank_enabled = bool(config.RERANK_URL)
    if rerank_enabled and candidates:
        try:
            order = await rerank(query, [chunk.text for chunk in candidates], limit)
            if order:
                selected = [candidates[index] for index, _score in order]
                score_by_offset = {
                    candidates[index].offset: float(score) for index, score in order
                }
            else:
                warnings.append("reranker returned no passages; used lexical ranking")
                score_by_offset = {}
        except (httpx.HTTPError, UpstreamResponseError) as exc:
            warnings.append(f"reranker unavailable ({type(exc).__name__}); used lexical ranking")
            score_by_offset = {}
    else:
        score_by_offset = {}
    return ArticlePassageResponse(
        article_id=article.article_id,
        query=query,
        title=article.title,
        citation=article.citation,
        provider=article.provider,
        content_level=article.content_level,
        extraction_method=article.extraction_method,
        license=article.license,
        warnings=warnings,
        passages=[
            ArticlePassage(
                section=chunk.section,
                text=chunk.text,
                offset=chunk.offset,
                end_offset=chunk.end_offset,
                rerank_score=score_by_offset.get(chunk.offset),
            )
            for chunk in selected
        ],
    )


async def article_read_response(article_id: str, offset: int = 0) -> ArticleReadResponse:
    try:
        offset = validate_offset(offset)
        article = await load_article(article_id)
    except ToolInputError as exc:
        return ArticleReadResponse(
            article_id=str(article_id),
            error=ToolError(code=exc.code, message=error_text("article_read", exc)),
        )
    except (httpx.HTTPError, UpstreamResponseError) as exc:
        code = getattr(exc, "code", "upstream_unavailable")
        return ArticleReadResponse(
            article_id=str(article_id),
            error=ToolError(code=code, message=f"article_read error [{code}]: {exc}"),
        )
    total = len(article.text)
    if offset >= total:
        return ArticleReadResponse(
            article_id=article.article_id,
            error=ToolError(
                code="offset_past_end",
                message=f"article_read error [offset_past_end]: offset {offset} is past {total}",
            ),
        )
    end = min(total, offset + config.ARTICLE_READ_WINDOW_CHARS)
    return ArticleReadResponse(
        article_id=article.article_id,
        title=article.title,
        citation=article.citation,
        provider=article.provider,
        content_level=article.content_level,
        extraction_method=article.extraction_method,
        license=article.license,
        warnings=article.warnings,
        text=article.text[offset:end],
        offset=offset,
        next_offset=end if end < total else None,
        total_length=total,
        end_of_document=end >= total,
    )


async def article_find(article_id: str, query: str, limit: int = 5) -> str:
    return render_article_passages(await article_find_response(article_id, query, limit))


async def article_read(article_id: str, offset: int = 0) -> str:
    return render_article_read(await article_read_response(article_id, offset))
