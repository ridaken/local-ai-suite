"""Hosted MCP gateway tests for the split, fail-closed HTTP surface."""

from starlette.testclient import TestClient

from mcp_gateway.server import build_app
from mcp_gateway.settings_store import SettingsStore

API_KEY = "m" * 48


def test_gateway_exposes_operational_mcp_and_versioned_api_routes(tmp_path):
    db = tmp_path / "state" / "settings.db"
    SettingsStore(db)
    app = build_app(api_key=API_KEY, settings=SettingsStore(db, read_only=True, initialize=False))
    paths = {getattr(route, "path", None) for route in app.routes}

    assert paths == {
        "/healthz",
        "/readyz",
        "/mcp",
        "/api/v1",
        "/api/v1/openapi.json",
        "/api/v1/kb/search",
        "/api/v1/kb/read",
        "/api/v1/web/search",
        "/api/v1/pubmed/search",
        "/api/v1/arxiv/search",
        "/api/v1/articles/find",
        "/api/v1/articles/read",
        "/api/v1/calculate",
    }

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200
        assert client.get("/").status_code == 404
        assert client.get("/settings").status_code == 404

        missing = client.get("/mcp")
        assert missing.status_code == 401
        assert missing.headers["www-authenticate"] == "Bearer"

        wrong = client.get("/mcp", headers={"Authorization": "Bearer wrong"})
        assert wrong.status_code == 401

        assert client.get("/api/v1").status_code == 401
        api = client.get(
            "/api/v1", headers={"Authorization": f"Bearer {API_KEY}"}
        )
        assert api.status_code == 200
        assert api.json()["openapi"] == "/api/v1/openapi.json"

        authenticated = client.get(
            "/mcp", headers={"Authorization": f"Bearer {API_KEY}"}
        )
        assert authenticated.status_code != 401
        assert authenticated.status_code != 404
