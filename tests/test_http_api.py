"""Client-neutral JSON API parity with the MCP tool surface."""

from starlette.testclient import TestClient

from mcp_gateway import http_api
from mcp_gateway.schemas import SearchResponse, SearchResult, search_error
from mcp_gateway.server import build_app
from mcp_gateway.settings_store import SettingsStore

API_KEY = "a" * 48
AUTH = {"Authorization": f"Bearer {API_KEY}"}


def _client(tmp_path) -> TestClient:
    db = tmp_path / "state" / "settings.db"
    SettingsStore(db)
    app = build_app(
        api_key=API_KEY,
        settings=SettingsStore(db, read_only=True, initialize=False),
    )
    return TestClient(app)


def test_api_discovery_and_openapi_are_authenticated(tmp_path):
    client = _client(tmp_path)
    assert client.get("/api/v1").status_code == 401
    index = client.get("/api/v1", headers=AUTH)
    assert index.status_code == 200
    assert {item["name"] for item in index.json()["operations"]} == {
        operation.name for operation in http_api.OPERATIONS
    }

    document = client.get("/api/v1/openapi.json", headers=AUTH)
    assert document.status_code == 200
    spec = document.json()
    assert spec["openapi"] == "3.1.0"
    assert "/api/v1/articles/find" in spec["paths"]
    assert "ArticlePassageResponse" in spec["components"]["schemas"]


def test_pubmed_api_returns_the_same_structured_shape(monkeypatch, tmp_path):
    async def fake_search(query, limit):
        assert (query, limit) == ("sepsis", 2)
        return SearchResponse(
            query=query,
            results=[
                SearchResult(
                    id="pubmed:1",
                    title="Guideline",
                    excerpt="",
                    source_kind="pubmed",
                    citation="https://pubmed.ncbi.nlm.nih.gov/1/",
                    article_id="pubmed:1",
                    abstract="Complete structured abstract",
                    available_content=["metadata", "abstract", "full_text"],
                )
            ],
        )

    monkeypatch.setattr(http_api.pubmed_mod, "pubmed_search_response", fake_search)
    client = _client(tmp_path)
    response = client.post(
        "/api/v1/pubmed/search",
        headers=AUTH,
        json={"query": "sepsis", "limit": 2},
    )
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["article_id"] == "pubmed:1"
    assert result["abstract"] == "Complete structured abstract"
    assert result["available_content"][-1] == "full_text"


def test_api_rejects_invalid_json_and_maps_typed_tool_errors(monkeypatch, tmp_path):
    async def unconfigured(query, limit):  # noqa: ARG001
        return search_error(query, "not_configured", "provider is not configured")

    monkeypatch.setattr(http_api.web_search_mod, "web_search_response", unconfigured)
    client = _client(tmp_path)
    invalid = client.post(
        "/api/v1/pubmed/search",
        headers={**AUTH, "Content-Type": "application/json"},
        content="not-json",
    )
    unavailable = client.post(
        "/api/v1/web/search",
        headers=AUTH,
        json={"query": "current event"},
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_request"
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "not_configured"
