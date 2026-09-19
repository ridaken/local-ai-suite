"""PubMed search and record retrieval through NCBI E-utilities.

Search is deliberately a candidate-selection operation: ESearch chooses PMIDs
and batched EFetch returns bibliographic metadata plus the real abstracts. Full
article text is retrieved only after selection by the article tools.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from xml.etree import ElementTree as ET

import httpx

from .. import config
from ..limits import (
    ToolInputError,
    UpstreamResponseError,
    clamp_limit,
    error_text,
    response_bytes,
    response_json,
    validate_query,
)
from ..schemas import SOURCE_PUBMED, SearchResponse, SearchResult, render_search, search_error


@dataclass(frozen=True)
class PubmedRecord:
    pmid: str
    title: str
    abstract: str
    authors: tuple[str, ...]
    journal: str
    pubdate: str
    pmcid: str | None
    doi: str | None


_pace_locks: dict[int, asyncio.Lock] = {}
_last_request: dict[int, float] = {}


def auth_params() -> dict[str, str]:
    params = {"tool": config.NCBI_TOOL}
    if config.NCBI_EMAIL:
        params["email"] = config.NCBI_EMAIL
    if config.NCBI_API_KEY:
        params["api_key"] = config.NCBI_API_KEY
    return params


async def _pace_ncbi() -> None:
    """Respect NCBI's per-IP request-start limit within this gateway process."""
    loop = asyncio.get_running_loop()
    key = id(loop)
    lock = _pace_locks.setdefault(key, asyncio.Lock())
    interval = 0.1 if config.NCBI_API_KEY else 1 / 3
    async with lock:
        delay = interval - (time.monotonic() - _last_request.get(key, 0.0))
        if delay > 0:
            await asyncio.sleep(delay)
        _last_request[key] = time.monotonic()


async def ncbi_get(
    client: httpx.AsyncClient, endpoint: str, params: dict[str, str]
) -> httpx.Response:
    """Rate-limited GET with bounded retry for transient NCBI failures."""
    url = f"{config.NCBI_BASE}/{endpoint}"
    for attempt in range(3):
        await _pace_ncbi()
        response = await client.get(
            url,
            params={**params, **auth_params()},
            headers={"User-Agent": config.USER_AGENT},
        )
        if response.status_code != 429 and response.status_code < 500:
            response.raise_for_status()
            return response
        if attempt < 2:
            retry_after = response.headers.get("retry-after", "")
            try:
                delay = min(5.0, max(0.1, float(retry_after)))
            except ValueError:
                delay = 0.5 * (2**attempt)
            await asyncio.sleep(delay)
    response.raise_for_status()
    return response


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return " ".join("".join(element.itertext()).split())


def parse_pubmed_xml(payload: bytes) -> list[PubmedRecord]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise UpstreamResponseError(
            "upstream_malformed", "NCBI returned malformed PubMed XML"
        ) from exc

    records: list[PubmedRecord] = []
    for article in root.findall(".//PubmedArticle"):
        pmid = _text(article.find(".//MedlineCitation/PMID"))
        if not pmid:
            continue
        abstract_parts = []
        for part in article.findall(".//Article/Abstract/AbstractText"):
            body = _text(part)
            if not body:
                continue
            label = (part.get("Label") or part.get("NlmCategory") or "").strip()
            abstract_parts.append(f"{label}: {body}" if label else body)
        authors = []
        for author in article.findall(".//Article/AuthorList/Author"):
            collective = _text(author.find("CollectiveName"))
            name = collective or " ".join(
                x for x in [_text(author.find("ForeName")), _text(author.find("LastName"))] if x
            )
            if name:
                authors.append(name)
        ids = {
            (node.get("IdType") or "").lower(): _text(node)
            for node in article.findall(".//PubmedData/ArticleIdList/ArticleId")
        }
        records.append(
            PubmedRecord(
                pmid=pmid,
                title=_text(article.find(".//Article/ArticleTitle")) or "(untitled)",
                abstract="\n\n".join(abstract_parts),
                authors=tuple(authors),
                journal=_text(article.find(".//Article/Journal/Title")),
                pubdate=_text(article.find(".//Article/Journal/JournalIssue/PubDate")),
                pmcid=ids.get("pmc") or None,
                doi=ids.get("doi") or None,
            )
        )
    return records


async def fetch_pubmed_records(
    client: httpx.AsyncClient, pmids: list[str]
) -> list[PubmedRecord]:
    response = await ncbi_get(
        client,
        "efetch.fcgi",
        {"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"},
    )
    return parse_pubmed_xml(response_bytes(response))


async def pubmed_search_response(query: str, limit: int = 5) -> SearchResponse:
    """Search PubMed and return abstracts suitable for article selection."""
    try:
        query = validate_query(query)
        limit = clamp_limit(limit, 5)
    except ToolInputError as exc:
        return search_error(str(query), exc.code, error_text("pubmed_search", exc))
    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT, follow_redirects=True) as client:
            search = await ncbi_get(
                client,
                "esearch.fcgi",
                {
                    "db": "pubmed",
                    "term": query,
                    "retmax": str(limit),
                    "retmode": "json",
                    "sort": "relevance",
                },
            )
            search_data = response_json(search)
            if not isinstance(search_data, dict):
                raise UpstreamResponseError("upstream_malformed", "NCBI returned an invalid shape")
            search_result = search_data.get("esearchresult")
            if not isinstance(search_result, dict):
                raise UpstreamResponseError(
                    "upstream_malformed", "NCBI returned an invalid search result"
                )
            ids = search_result.get("idlist", [])
            if not isinstance(ids, list) or not all(isinstance(value, str) for value in ids):
                raise UpstreamResponseError("upstream_malformed", "NCBI returned invalid IDs")
            if not ids:
                return SearchResponse(query=query, results=[])
            records = await fetch_pubmed_records(client, ids)
    except httpx.HTTPError:
        return search_error(
            query,
            "upstream_unavailable",
            "pubmed_search error [upstream_unavailable]: NCBI request failed.",
        )
    except UpstreamResponseError as exc:
        return search_error(query, exc.code, error_text("pubmed_search", exc))

    by_id = {record.pmid: record for record in records}
    results = []
    warnings = []
    for pmid in ids:
        record = by_id.get(pmid)
        if record is None:
            warnings.append(f"PMID {pmid} was omitted from NCBI EFetch")
            continue
        byline = (
            record.authors[0] + (" et al." if len(record.authors) > 1 else "")
            if record.authors
            else ""
        )
        available = ["metadata"]
        if record.abstract:
            available.append("abstract")
        if record.pmcid:
            available.append("full_text")
        results.append(
            SearchResult(
                id=f"pubmed:{pmid}",
                article_id=f"pubmed:{pmid}",
                title=record.title.rstrip("."),
                excerpt=", ".join(x for x in [byline, record.journal, record.pubdate] if x),
                abstract=record.abstract or None,
                available_content=available,
                source_kind=SOURCE_PUBMED,
                citation=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            )
        )
    return SearchResponse(query=query, results=results, warnings=warnings)


def render(response: SearchResponse) -> str:
    return render_search(
        response,
        heading="PubMed results",
        footer=(
            "Search results are candidates, not proof. Select relevant article_id values and "
            "call article_find/article_read before citing article contents."
        ),
    )


async def pubmed_search(query: str, limit: int = 5) -> str:
    """Text-only entry point (stdio clients and tests)."""
    return render(await pubmed_search_response(query, limit))
