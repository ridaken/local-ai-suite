"""Readable fallbacks stay concise without reducing structured responses."""

from mcp_gateway.schemas import SearchResponse, SearchResult, render_search


def test_long_abstract_is_previewed_only_in_text_rendering():
    abstract = "word " * 600
    response = SearchResponse(
        query="topic",
        results=[
            SearchResult(
                id="pubmed:1",
                title="Paper",
                excerpt="",
                source_kind="pubmed",
                citation="https://pubmed.ncbi.nlm.nih.gov/1/",
                article_id="pubmed:1",
                abstract=abstract,
                available_content=["metadata", "abstract"],
            )
        ],
    )

    rendered = render_search(response, heading="Results")
    assert "abstract preview truncated" in rendered
    assert len(rendered) < len(abstract)
    assert response.results[0].abstract == abstract
