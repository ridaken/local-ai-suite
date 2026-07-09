"""Curated ZIM recommendations for the hosted gateway admin UI.

Recommendations are human-facing only: they help a user pick useful starter
ZIMs, then reuse the existing download manager path so completed files refresh
library.xml and become searchable through kb_search.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from . import catalog as catalog_client
from .catalog import CatalogEntry

SearchCatalog = Callable[[str, str, int], Awaitable[list[CatalogEntry]]]


@dataclass(frozen=True)
class Recommendation:
    key: str
    label: str
    rationale: str
    query: str
    lang: str = "eng"
    count: int = 30
    prefer_terms: tuple[str, ...] = ()
    avoid_terms: tuple[str, ...] = ("mini",)


@dataclass(frozen=True)
class ResolvedRecommendation:
    recommendation: Recommendation
    entry: CatalogEntry | None
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.entry is not None and self.entry.download_url is not None


RECOMMENDATIONS: tuple[Recommendation, ...] = (
    Recommendation(
        key="wikipedia-en",
        label="English Wikipedia",
        rationale="Broad offline reference coverage for general questions.",
        query="wikipedia english nopic",
        prefer_terms=("wikipedia", "en", "nopic", "all"),
    ),
    Recommendation(
        key="stackoverflow",
        label="Stack Overflow",
        rationale="Programming Q&A that works well with lexical kb_search.",
        query="stackoverflow",
        prefer_terms=("stackoverflow", "stack_exchange"),
    ),
    Recommendation(
        key="devdocs-python",
        label="Python documentation",
        rationale="Compact Python reference docs for coding workflows.",
        query="devdocs python",
        prefer_terms=("devdocs", "python"),
    ),
    Recommendation(
        key="devdocs-web",
        label="Web documentation",
        rationale="JavaScript or web platform docs for frontend and scripting questions.",
        query="devdocs javascript mdn",
        prefer_terms=("devdocs", "javascript", "mdn", "web"),
    ),
)


def _haystack(entry: CatalogEntry) -> str:
    return " ".join(
        (
            entry.name,
            entry.title,
            entry.description,
            entry.category,
            entry.tags,
            entry.download_url or "",
        )
    ).lower()


def score_entry(entry: CatalogEntry, recommendation: Recommendation) -> int:
    """Score catalog results for recommendation quality.

    Invalid download candidates receive a negative score so callers can filter
    them out. Positive scores prefer searchable, English, practical ZIMs without
    requiring exact upstream filenames.
    """
    if not entry.download_url:
        return -1_000

    text = _haystack(entry)
    score = 0
    if entry.has_fulltext_index is True:
        score += 500
    elif entry.has_fulltext_index is False:
        score -= 500

    if entry.language.lower() in (recommendation.lang.lower(), "en", "english"):
        score += 200
    elif entry.language:
        score -= 50

    for term in recommendation.prefer_terms:
        if term.lower() in text:
            score += 80

    for term in recommendation.avoid_terms:
        if term.lower() in text:
            score -= 250

    if "nopic" in text:
        score += 60
    if "_ftindex:yes" in text:
        score += 40

    # Smaller variants are more practical starter recommendations when two
    # otherwise similar catalog entries match.
    if entry.size_bytes is not None:
        score -= min(entry.size_bytes // (1024 * 1024 * 1024), 80)

    return score


def best_entry(
    entries: list[CatalogEntry], recommendation: Recommendation
) -> CatalogEntry | None:
    scored = [(score_entry(entry, recommendation), entry) for entry in entries]
    valid = [(score, entry) for score, entry in scored if score >= 0]
    if not valid:
        return None
    return max(valid, key=lambda item: item[0])[1]


async def resolve_recommendation(
    recommendation: Recommendation,
    *,
    search_catalog: SearchCatalog | None = None,
) -> ResolvedRecommendation:
    search = search_catalog or catalog_client.search_catalog
    try:
        entries = await search(recommendation.query, recommendation.lang, recommendation.count)
    except httpx.HTTPError as exc:
        return ResolvedRecommendation(
            recommendation=recommendation,
            entry=None,
            error=f"Catalog unreachable ({type(exc).__name__}).",
        )
    return ResolvedRecommendation(
        recommendation=recommendation,
        entry=best_entry(entries, recommendation),
    )


async def resolve_recommendations(
    *,
    search_catalog: SearchCatalog | None = None,
) -> list[ResolvedRecommendation]:
    return await asyncio.gather(
        *[
            resolve_recommendation(recommendation, search_catalog=search_catalog)
            for recommendation in RECOMMENDATIONS
        ]
    )
