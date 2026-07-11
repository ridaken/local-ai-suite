"""Lexical retrieval over the offline knowledge base (kiwix-serve full-text search).

This is the Phase 1 mechanism, factored out so both the kb_search tool and the
Phase 2 hybrid pipeline can use it without a circular import.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

import httpx

from mcp_gateway import config

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Conversational scaffolding a model tends to copy from the user's phrasing
# straight into its lexical query (e.g. it turns "tell me a fun fact about the
# Roman Empire" into the query "Roman Empire fun facts"). Two kinds of words:
# classic stopwords, and "meta framing" words (fun/facts/random/trivia...).
# The framing words are the real problem for a Xapian-backed kiwix index: they
# are rare, high-weight tokens that occur as literal "Fun Facts" section headers
# and citations across unrelated articles (Broccoli, Paris...), so an OR-ish
# match floats those to the top and buries the actual subject. Stripping this
# scaffolding before search lets the entity terms rank. Only used for the
# *lexical* pattern and excerpt centering — the display query and the dense
# embedding query keep the user's full phrasing.
QUERY_FILLER = frozenset(
    # question / command openers
    "tell me give show list find explain describe define what whats which who "
    "whose where when why how is are was were do does did can could would will "
    "please i want need know get share".split()
    # articles / prepositions / conjunctions
    + "a an the some any of about for on to in with and or as at by from".split()
    # trivia / "info about X" framing — the high-IDF offenders
    + "fun facts fact random trivia interesting cool thing things info "
    "information detail details more something anything quick".split()
)
_FILLER_STRIP = ".,!?;:\"'()[]"


def normalize_query(query: str) -> str:
    """Strip conversational scaffolding so lexical search ranks on content terms.

    Falls back to the original query if stripping would leave nothing (e.g. the
    query really is "The Who" or "facts"), so a legitimate all-filler title still
    searches. Token punctuation is only stripped for the membership test — kept
    tokens are emitted verbatim so "COVID-19" survives intact."""
    kept = [w for w in query.split() if w.strip(_FILLER_STRIP).lower() not in QUERY_FILLER]
    return " ".join(kept).strip() or query.strip()


@dataclass
class Hit:
    title: str
    url: str
    snippet: str


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()


def _element_text(el: ET.Element | None) -> str:
    """All text inside an element, including the text of and after child tags.

    Kiwix wraps matched terms in the snippet in <b>…</b>, so <description> has
    child elements. ElementTree's .text / findtext return only the text *before
    the first child* — for a highlighted snippet that is just the leading "…",
    which is why snippets came back effectively empty. itertext() flattens the
    element's whole text content (the <b> tags drop out as we only keep text)."""
    if el is None:
        return ""
    return "".join(el.itertext())


def _absolute(url: str) -> str:
    # Citation links go to humans/the model, so build them on the public base,
    # not the internal service URL the gateway fetches through.
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return f"{config.KIWIX_PUBLIC_URL}/{url.lstrip('/')}"


async def kiwix_search(query: str, limit: int, books: list[str] | None = None) -> list[Hit]:
    """Query kiwix-serve full-text search. Raises httpx.HTTPError if unreachable.

    `books`, when given, restricts the search to those book names (repeated
    `books.filter.name=` params — libkiwix's InternalServer collects every
    value via `get_arguments`, so this searches the union of the listed books).
    This is how the admin UI's per-book enable/disable toggle takes effect. When
    omitted, falls back to the single-book KIWIX_BOOK filter from config (or no
    filter at all).

    NOTE: the parameter is `books.filter.name`, not `books.name`. kiwix-serve
    rejects an unknown `books.*` filter key with HTTP 400 (not an empty result),
    so getting this wrong makes every filtered search raise rather than degrade.
    """
    if books == []:
        return []

    params: dict[str, str | list[str]] = {
        "pattern": normalize_query(query),
        "pageLength": str(limit),
        "format": "xml",
    }
    if books is not None:
        params["books.filter.name"] = books
    elif config.KIWIX_BOOK:
        params["books.filter.name"] = config.KIWIX_BOOK

    async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(
            f"{config.KIWIX_URL}/search",
            params=params,
            headers={"User-Agent": config.USER_AGENT},
        )
    resp.raise_for_status()

    root = ET.fromstring(resp.content)
    hits: list[Hit] = []
    for item in root.findall(".//item")[:limit]:
        hits.append(
            Hit(
                title=_clean(item.findtext("title")) or "(untitled)",
                url=_absolute(_clean(item.findtext("link"))),
                snippet=_clean(_element_text(item.find("description"))),
            )
        )
    return hits
