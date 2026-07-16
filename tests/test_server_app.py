"""Hosted MCP gateway tests for the split, fail-closed HTTP surface."""

from starlette.testclient import TestClient

from mcp_gateway.server import build_app
from mcp_gateway.settings_store import SettingsStore

API_KEY = "m" * 48


def test_mcp_gateway_exposes_only_operational_and_mcp_routes(tmp_path):
    db = tmp_path / "state" / "settings.db"
    SettingsStore(db)
    app = build_app(api_key=API_KEY, settings=SettingsStore(db, read_only=True, initialize=False))
    paths = {getattr(route, "path", None) for route in app.routes}

    assert paths == {"/healthz", "/readyz", "/mcp"}

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

        authenticated = client.get(
            "/mcp", headers={"Authorization": f"Bearer {API_KEY}"}
        )
        assert authenticated.status_code != 401
        assert authenticated.status_code != 404
