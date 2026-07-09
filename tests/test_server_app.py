"""Tests for the merged HTTP app (admin UI + MCP-over-streamable-HTTP in one
Starlette app). Full MCP protocol handshake isn't exercised here — that's
covered by the Phase 3 acceptance gate's manual pi/inspector check — this just
confirms both surfaces are actually mounted and reachable in one process."""

from starlette.testclient import TestClient

from mcp_gateway.server import build_app


def test_admin_and_mcp_routes_share_one_app(tmp_path, monkeypatch):
    # mcp.session_manager.run() may only be entered once per process (FastMCP
    # enforces this), so build_app() — like production's main() — is exercised
    # exactly once here rather than once per test.
    monkeypatch.setattr("mcp_gateway.config.ZIM_DIR", str(tmp_path / "zim"))
    monkeypatch.setattr("mcp_gateway.config.SETTINGS_DB", str(tmp_path / "settings.db"))
    monkeypatch.setattr(
        "mcp_gateway.config.LIBRARY_XML_PATH", str(tmp_path / "zim" / "library.xml")
    )

    app = build_app()
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/mcp" in paths

    with TestClient(app) as client:
        resp = client.get("/")
        assert resp.status_code == 200
        assert "local-ai-suite admin" in resp.text

        resp = client.get("/settings")
        assert resp.status_code == 200

        # A bare GET without the streamable-http session/accept headers is
        # expected to be rejected by the MCP layer, but it must not 404 —
        # that would mean the route isn't mounted at all.
        resp = client.get("/mcp")
        assert resp.status_code != 404
