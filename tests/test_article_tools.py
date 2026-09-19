"""Article selection, extraction, relevance, evidence, and paging."""

import asyncio

from mcp_gateway import config
from mcp_gateway.tools import article as article_mod
from mcp_gateway.tools.article import LoadedArticle

_JATS = b"""<article><front><article-meta>
  <title-group><article-title>Sepsis Trial</article-title></title-group>
  <abstract><p>Structured abstract text.</p></abstract>
  <permissions><license><license-p>CC BY 4.0</license-p></license></permissions>
</article-meta></front><body>
  <sec><title>Methods</title><p>Patients received usual care.</p></sec>
  <sec><title>Results</title><p>Mortality was lower in the intervention arm.</p>
    <table-wrap><label>Table 1</label><caption><p>Outcomes</p></caption>
      <table><tr><th>Arm</th><th>Deaths</th></tr><tr><td>Intervention</td><td>4</td></tr></table>
    </table-wrap>
  </sec>
</body><back><ref-list><ref>Example reference.</ref></ref-list></back></article>"""


def test_parse_pmc_jats_keeps_sections_tables_and_license():
    title, text, license_text = article_mod.parse_pmc_jats(_JATS)
    assert title == "Sepsis Trial"
    assert "## Methods" in text
    assert "Mortality was lower" in text
    assert "Arm | Deaths" in text
    assert "Example reference" in text
    assert license_text == "CC BY 4.0"


def test_parse_arxiv_html_removes_chrome_and_keeps_math():
    html = b"""<html><body><nav>menu</nav><article><h1>Paper</h1>
    <p>An equation <math alttext="x+y"></math> is useful.</p></article>
    <script>bad()</script></body></html>"""
    text = article_mod.parse_arxiv_html(html)
    assert "Paper" in text
    assert "x+y" in text
    assert "menu" not in text
    assert "bad()" not in text


def test_article_find_reports_full_text_passages_and_offsets(monkeypatch):
    article = LoadedArticle(
        article_id="pubmed:1",
        provider="pubmed",
        title="Study",
        citation="https://pubmed.ncbi.nlm.nih.gov/1/",
        content_level="full_text",
        extraction_method="pmc_jats_xml",
        text=(
            "# Study\n\n## Methods\n\ncontrol group\n\n## Results\n\n"
            "The intervention lowered mortality."
        ),
    )

    async def fake_load(_article_id):
        return article

    monkeypatch.setattr(article_mod, "load_article", fake_load)
    monkeypatch.setattr(config, "RERANK_URL", "")
    response = asyncio.run(article_mod.article_find_response("pubmed:1", "lowered mortality", 2))
    assert response.error is None
    assert response.content_level == "full_text"
    assert response.passages
    assert "lowered mortality" in response.passages[0].text
    assert response.passages[0].end_offset > response.passages[0].offset


def test_article_find_excludes_reference_chunks(monkeypatch):
    article = LoadedArticle(
        article_id="pubmed:1",
        provider="pubmed",
        title="Guideline",
        citation="https://pubmed.ncbi.nlm.nih.gov/1/",
        content_level="full_text",
        extraction_method="pmc_jats_xml",
        text=(
            "## Recommendations\n\nTreat septic shock with norepinephrine.\n\n"
            + ("supporting context " * 140)
            + "\n\n## References\n\n"
            + ("reference-only septic shock norepinephrine treatment guideline " * 120)
        ),
    )

    async def fake_load(_article_id):
        return article

    monkeypatch.setattr(article_mod, "load_article", fake_load)
    monkeypatch.setattr(config, "RERANK_URL", "")
    response = asyncio.run(
        article_mod.article_find_response(
            "pubmed:1", "septic shock norepinephrine treatment guideline", 5
        )
    )
    assert response.passages
    assert all(passage.section != "References" for passage in response.passages)
    assert all("reference-only" not in passage.text for passage in response.passages)
    assert response.passages[0].section == "Recommendations"


def test_article_read_pages_and_labels_abstract_fallback(monkeypatch):
    article = LoadedArticle(
        article_id="pubmed:2",
        provider="pubmed",
        title="Abstract only",
        citation="https://pubmed.ncbi.nlm.nih.gov/2/",
        content_level="abstract",
        extraction_method="pubmed_xml",
        text="x" * 700,
        warnings=["No PMCID"],
    )

    async def fake_load(_article_id):
        return article

    monkeypatch.setattr(article_mod, "load_article", fake_load)
    monkeypatch.setattr(config, "ARTICLE_READ_WINDOW_CHARS", 500)
    response = asyncio.run(article_mod.article_read_response("pubmed:2"))
    assert response.content_level == "abstract"
    assert response.next_offset == 500
    assert response.end_of_document is False
    assert response.warnings == ["No PMCID"]
    rendered = article_mod.render_article_read(response)
    assert "abstract" in rendered
    assert "offset=500" in rendered


def test_article_ids_reject_urls_and_bad_offsets_without_loading(monkeypatch):
    bad_id = asyncio.run(article_mod.article_read_response("https://evil.example/paper"))
    assert bad_id.error.code == "invalid_article_id"

    async def must_not_load(_article_id):
        raise AssertionError("invalid input must be rejected first")

    monkeypatch.setattr(article_mod, "load_article", must_not_load)
    bad_offset = asyncio.run(article_mod.article_read_response("pubmed:1", -1))
    assert bad_offset.error.code == "invalid_offset"


def test_cache_is_bounded_and_expires(monkeypatch):
    article_mod._cache.clear()
    article_mod._cache_bytes = 0
    monkeypatch.setattr(config, "ARTICLE_CACHE_MAX_ITEMS", 1)
    monkeypatch.setattr(config, "ARTICLE_CACHE_MAX_BYTES", 1000)
    monkeypatch.setattr(config, "ARTICLE_CACHE_TTL_SECONDS", 60)
    first = LoadedArticle("pubmed:1", "pubmed", "One", "u1", "abstract", "x", "one")
    second = LoadedArticle("pubmed:2", "pubmed", "Two", "u2", "abstract", "x", "two")
    article_mod._cache_put(first)
    article_mod._cache_put(second)
    assert article_mod._cache_get("pubmed:1") is None
    assert article_mod._cache_get("pubmed:2") is second
