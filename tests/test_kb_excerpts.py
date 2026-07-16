"""Tests for query-relevant excerpting and kb_search excerpt enrichment."""

import asyncio

from mcp_gateway.tools import kb_search as kb_search_mod
from mcp_gateway.tools.kb_read import best_excerpt
from retrieval.hybrid import Candidate

# A page whose densest keyword region is a shared navbox (the real failure), with
# the actual answer elsewhere in the body.
_ARTICLE = (
    "Hypothyroidism is a condition of the thyroid. "
    "Symptoms of hypothyroidism include tiredness, weight gain, dry skin, "
    "feeling cold, and constipation. "
    "Navbox: Hypothyroid myopathy KDSS Hoffmann syndrome Graves disease "
    "Abadie sign Boston sign Dalrymple sign lid lag Griffith sign."
)


def test_best_excerpt_centers_on_query_term_not_lead():
    ex = best_excerpt(_ARTICLE, "hypothyroidism symptoms", 120)
    assert "tiredness" in ex and "weight gain" in ex
    # Meaningful term drove placement, not the generic navbox tail.
    assert "Abadie" not in ex


def test_best_excerpt_ignores_stopwords_for_placement():
    # "of" appears first but is a stopword; placement should use "symptoms".
    ex = best_excerpt(_ARTICLE, "what are the symptoms", 100)
    assert "Symptoms of hypothyroidism include" in ex or "symptoms" in ex.lower()


def test_best_excerpt_ignores_conversational_filler_for_placement():
    # "facts" is filler, and here appears only in the navbox tail. It must not be
    # treated as a content term and pin the excerpt to that tail; the real content
    # term ("symptoms") should still drive placement.
    article = _ARTICLE + " Interesting facts and trivia about the Abadie sign."
    ex = best_excerpt(article, "hypothyroidism fun facts symptoms", 120)
    assert "tiredness" in ex
    assert "trivia about the Abadie" not in ex


def test_best_excerpt_falls_back_to_lead_when_no_term_matches():
    ex = best_excerpt(_ARTICLE, "zzzquux nonsense", 40)
    assert ex.startswith("Hypothyroidism is a condition")
    assert ex.endswith("…")


def test_best_excerpt_empty_text():
    assert best_excerpt("", "anything", 100) == ""


def test_enrich_replaces_kiwix_snippet_with_fetched_excerpt(monkeypatch):
    cands = [
        Candidate(title="Hypothyroidism", text="...navbox junk...",
                  citation="http://kiwix:8080/content/b/A/Hypothyroidism", source="kb"),
        Candidate(title="chunk", text="curated text", citation="repo/x.py", source="curated"),
    ]

    async def fake_excerpt(source, query, size):
        return "Symptoms include tiredness and weight gain."

    monkeypatch.setattr(kb_search_mod, "article_excerpt", fake_excerpt)
    asyncio.run(kb_search_mod._enrich_kb_excerpts("hypothyroidism symptoms", cands))

    assert cands[0].text == "Symptoms include tiredness and weight gain."
    assert cands[1].text == "curated text"  # non-kb candidate untouched


def test_enrich_keeps_original_snippet_when_fetch_fails(monkeypatch):
    cands = [
        Candidate(title="A", text="original snippet",
                  citation="http://kiwix:8080/content/b/A/A", source="kb"),
    ]

    async def fake_excerpt(source, query, size):
        return None  # fetch failed

    monkeypatch.setattr(kb_search_mod, "article_excerpt", fake_excerpt)
    asyncio.run(kb_search_mod._enrich_kb_excerpts("q", cands))

    assert cands[0].text == "original snippet"


def test_enrich_survives_fetch_exception(monkeypatch):
    cands = [
        Candidate(title="A", text="original", citation="http://kiwix:8080/content/b/A/A",
                  source="kb"),
    ]

    async def boom(source, query, size):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(kb_search_mod, "article_excerpt", boom)
    # gather(return_exceptions=True) means one bad fetch must not sink the search.
    asyncio.run(kb_search_mod._enrich_kb_excerpts("q", cands))
    assert cands[0].text == "original"
