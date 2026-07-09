import asyncio

import httpx

from mcp_gateway import recommendations
from mcp_gateway.catalog import CatalogEntry
from mcp_gateway.recommendations import (
    Recommendation,
    best_entry,
    resolve_recommendation,
    resolve_recommendations,
)


def _entry(
    name: str,
    *,
    title: str | None = None,
    language: str = "eng",
    tags: str = "_ftindex:yes",
    download_url: str | None = "default",
    has_fulltext_index: bool | None = True,
    size_bytes: int | None = 1024,
) -> CatalogEntry:
    return CatalogEntry(
        uuid=name,
        name=name,
        title=title or name,
        description="",
        language=language,
        category="",
        tags=tags,
        article_count=100,
        media_count=0,
        updated="",
        size_bytes=size_bytes,
        download_url=(
            f"https://example.org/{name}.zim" if download_url == "default" else download_url
        ),
        has_fulltext_index=has_fulltext_index,
    )


def test_best_entry_prefers_fulltext_english_over_mini_no_index():
    rec = Recommendation(
        key="wiki",
        label="Wikipedia",
        rationale="",
        query="wikipedia",
        prefer_terms=("wikipedia", "nopic"),
    )
    mini = _entry(
        "wikipedia_en_all_mini",
        title="Wikipedia mini",
        tags="_ftindex:no;mini",
        has_fulltext_index=False,
    )
    nopic = _entry("wikipedia_en_all_nopic", title="Wikipedia nopic")

    assert best_entry([mini, nopic], rec) == nopic


def test_best_entry_prefers_nopic_for_practical_starter_download():
    rec = Recommendation(
        key="wiki",
        label="Wikipedia",
        rationale="",
        query="wikipedia",
        prefer_terms=("wikipedia", "nopic"),
    )
    full = _entry(
        "wikipedia_en_all_maxi",
        title="Wikipedia full",
        size_bytes=120 * 1024 * 1024 * 1024,
    )
    nopic = _entry(
        "wikipedia_en_all_nopic",
        title="Wikipedia nopic",
        size_bytes=55 * 1024 * 1024 * 1024,
    )

    assert best_entry([full, nopic], rec) == nopic


def test_best_entry_returns_none_when_no_valid_download_exists():
    rec = Recommendation(key="python", label="Python", rationale="", query="python")
    no_download = _entry("devdocs_python", download_url=None)

    assert best_entry([no_download], rec) is None


def test_resolve_recommendation_returns_unavailable_on_catalog_error():
    rec = Recommendation(key="python", label="Python", rationale="", query="python")

    async def fake_search(query: str, lang: str, count: int):
        raise httpx.ConnectError("offline")

    result = asyncio.run(resolve_recommendation(rec, search_catalog=fake_search))

    assert result.entry is None
    assert result.available is False
    assert "Catalog unreachable" in result.error


def test_resolve_recommendations_runs_catalog_searches_concurrently(monkeypatch):
    recs = (
        Recommendation(key="one", label="One", rationale="", query="one"),
        Recommendation(key="two", label="Two", rationale="", query="two"),
    )
    active = 0
    max_active = 0

    async def fake_search(query: str, lang: str, count: int):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return [_entry(query)]

    monkeypatch.setattr(recommendations, "RECOMMENDATIONS", recs)

    results = asyncio.run(resolve_recommendations(search_catalog=fake_search))

    assert [result.recommendation.key for result in results] == ["one", "two"]
    assert max_active == 2
