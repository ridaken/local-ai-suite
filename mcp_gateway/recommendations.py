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
    count: int = 50
    # Preferred canonical ZIM names, in priority order. The OPDS `q` search only
    # matches whole tokens (e.g. "stack" hits stackoverflow.com_en_all, but the
    # multi-word "wikipedia english nopic" matches nothing), so we search on one
    # broad token and pin the exact ZIM here. Falls back to score_entry ranking
    # when none of these are present.
    match_names: tuple[str, ...] = ()
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
        rationale="Broad offline reference coverage for general questions. Full-text indexed.",
        query="wikipedia",
        match_names=("wikipedia_en_all",),
        prefer_terms=("wikipedia", "en", "all"),
    ),
    Recommendation(
        key="stackoverflow",
        label="Stack Overflow",
        rationale=(
            "Programming Q&A — the canonical coding corpus. Note: the Kiwix build "
            "ships without a full-text index, so kb_search matches titles only."
        ),
        query="stack",
        match_names=("stackoverflow.com_en_all",),
        prefer_terms=("stackoverflow", "stack_exchange"),
    ),
    Recommendation(
        key="python-docs",
        label="Python documentation",
        rationale="Official Python docs (docs.python.org). Full-text indexed.",
        query="python",
        match_names=("docs.python.org_en_all",),
        prefer_terms=("python", "docs"),
    ),
    Recommendation(
        key="devdocs-web",
        label="Web / JavaScript documentation",
        rationale=(
            "DevDocs JavaScript reference for frontend work. No full-text index; "
            "best browsed or used as a curated vector source."
        ),
        query="javascript",
        match_names=("devdocs_en_javascript",),
        prefer_terms=("devdocs", "javascript", "web"),
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


def _dedupe(entries: list[CatalogEntry]) -> list[CatalogEntry]:
    """The catalog feed repeats the same ZIM (one row per flavour/mirror). Keep
    the first occurrence of each name so scoring/matching isn't skewed by dupes."""
    unique: dict[str, CatalogEntry] = {}
    for entry in entries:
        if entry.name not in unique:
            unique[entry.name] = entry
    return list(unique.values())


def best_entry(
    entries: list[CatalogEntry], recommendation: Recommendation
) -> CatalogEntry | None:
    """Best downloadable match by score.

    Only entries without a download URL are excluded — a missing full-text index
    (common for Stack Overflow / DevDocs ZIMs) lowers the score but must not make
    the recommendation vanish, or the whole tab reads "unavailable". The UI badges
    the index status so the tradeoff is visible.
    """
    downloadable = [e for e in entries if e.download_url]
    if not downloadable:
        return None
    return max(downloadable, key=lambda entry: score_entry(entry, recommendation))


def pick_entry(
    entries: list[CatalogEntry], recommendation: Recommendation
) -> CatalogEntry | None:
    """Resolve a recommendation to one catalog entry: prefer an exact (then
    substring) match against the recommendation's pinned canonical names, and
    otherwise fall back to the best-scored downloadable entry."""
    downloadable = [e for e in _dedupe(entries) if e.download_url]
    for wanted in recommendation.match_names:
        for entry in downloadable:
            if entry.name == wanted:
                return entry
        for entry in downloadable:
            if wanted in entry.name:
                return entry
    return best_entry(downloadable, recommendation)


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
        entry=pick_entry(entries, recommendation),
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
