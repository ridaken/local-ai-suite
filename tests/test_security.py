"""Authentication, browser, credential, and signed-action boundaries."""

import pytest
from starlette.testclient import TestClient

from mcp_gateway import admin, config, server
from mcp_gateway.downloads import DownloadManager
from mcp_gateway.security import AdminSecurity
from mcp_gateway.settings_store import SettingsStore

ADMIN_TOKEN = "a" * 48
MCP_KEY = "m" * 48


def _admin_client(tmp_path, *, secure=False):
    zim_dir = tmp_path / "zim"
    zim_dir.mkdir()
    app = admin.build_admin_app(
        settings=SettingsStore(tmp_path / "state" / "settings.db"),
        download_manager=DownloadManager(zim_dir),
        zim_dir=str(zim_dir),
        library_xml_path=str(zim_dir / "library.xml"),
        admin_token=ADMIN_TOKEN,
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
        cookie_secure=secure,
    )
    return TestClient(app), app


def _login(client):  # noqa: ANN001
    return client.post("/login", data={"token": ADMIN_TOKEN}, follow_redirects=False)


def _csrf(client, app):  # noqa: ANN001
    session_id = client.cookies.get("las_admin_session")
    return app.state.admin_security.sessions[session_id].csrf_token


def test_secret_loading_rejects_direct_and_file_conflict(tmp_path, monkeypatch):
    secret_file = tmp_path / "secret"
    secret_file.write_text("file-value", encoding="utf-8")
    monkeypatch.setenv("EXAMPLE_TOKEN", "direct-value")
    monkeypatch.setenv("EXAMPLE_TOKEN_FILE", str(secret_file))

    with pytest.raises(ValueError, match="only one"):
        config._secret("EXAMPLE_TOKEN")


def test_secret_loading_from_file_does_not_include_newline(tmp_path, monkeypatch):
    secret_file = tmp_path / "secret"
    secret_file.write_text("x" * 48 + "\n", encoding="utf-8")
    monkeypatch.delenv("EXAMPLE_TOKEN", raising=False)
    monkeypatch.setenv("EXAMPLE_TOKEN_FILE", str(secret_file))
    assert config._secret("EXAMPLE_TOKEN") == "x" * 48


@pytest.mark.parametrize("value", ["", "short", "change-me", "password"])
def test_required_credentials_fail_closed(monkeypatch, value):
    monkeypatch.setattr(config, "ADMIN_TOKEN", value)
    monkeypatch.setattr(config, "MCP_API_KEY", "m" * 48)
    monkeypatch.setattr(config, "MCPO_API_KEY", "")
    with pytest.raises(ValueError):
        config.validate_http_security(admin=True, mcp=True)


def test_duplicate_credentials_are_rejected(monkeypatch):
    shared = "s" * 48
    monkeypatch.setattr(config, "ADMIN_TOKEN", shared)
    monkeypatch.setattr(config, "MCP_API_KEY", shared)
    monkeypatch.setattr(config, "MCPO_API_KEY", "")
    with pytest.raises(ValueError, match="distinct"):
        config.validate_http_security(admin=True, mcp=True)


def test_distinct_strong_credentials_are_valid(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "a" * 48)
    monkeypatch.setattr(config, "MCP_API_KEY", "m" * 48)
    monkeypatch.setattr(config, "MCPO_API_KEY", "l" * 48)
    monkeypatch.setattr(config, "MCP_ALLOWED_HOSTS", ["localhost:*"])
    config.validate_http_security(admin=True, mcp=True, mcpo=True)


def test_stdio_transport_remains_credential_free(monkeypatch):
    called = []
    monkeypatch.delenv("LAS_TRANSPORT", raising=False)
    monkeypatch.setattr(config, "MCP_API_KEY", "")
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: called.append(kwargs))

    server.main()

    assert called == [{"transport": "stdio"}]


def test_admin_login_cookie_headers_and_session_fixation(tmp_path):
    client, _app = _admin_client(tmp_path, secure=True)
    client.cookies.set(
        "las_admin_session", "attacker-chosen", domain="testserver.local", path="/"
    )

    response = _login(client)

    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Secure" in cookie
    assert client.cookies.get("las_admin_session") != "attacker-chosen"
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_admin_login_logout_and_expiration(tmp_path):
    client, app = _admin_client(tmp_path)
    assert client.get("/", follow_redirects=False).status_code == 303
    assert _login(client).status_code == 303
    assert client.get("/").status_code == 200

    session_id = client.cookies.get("las_admin_session")
    app.state.admin_security.sessions[session_id].expires_at = 0
    assert client.get("/", follow_redirects=False).status_code == 303

    assert _login(client).status_code == 303
    logout = client.post(
        "/logout", data={"csrf_token": _csrf(client, app)}, follow_redirects=False
    )
    assert logout.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 303


def test_admin_rejects_hostile_host_and_origin(tmp_path):
    client, app = _admin_client(tmp_path)
    assert client.get("/login", headers={"Host": "evil.example"}).status_code == 400
    assert (
        client.post(
            "/login",
            data={"token": ADMIN_TOKEN},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )

    _login(client)
    response = client.post(
        "/settings/update",
        data={"csrf_token": _csrf(client, app), "mode": "lexical"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "path",
    ["/logout", "/sources/toggle", "/sources/delete", "/sources/download", "/settings/update"],
)
def test_every_admin_mutation_requires_authentication_and_csrf(tmp_path, path):
    client, app = _admin_client(tmp_path)
    assert client.post(path, data={}).status_code == 401

    _login(client)
    assert client.post(path, data={}).status_code == 403
    assert client.post(path, data={"csrf_token": "wrong"}).status_code == 403
    assert _csrf(client, app)


def test_download_actions_reject_tampering_replay_expiration_and_cross_session():
    now = [1_000]
    security = AdminSecurity(
        ADMIN_TOKEN,
        allowed_hosts=["localhost"],
        allowed_origins=["http://localhost"],
        now_fn=lambda: now[0],
    )
    first = security.create_session()
    second = security.create_session()
    action = security.issue_download_action(
        first,
        url="https://download.kiwix.org/foo.zim",
        filename="foo.zim",
        expected_bytes=4,
    )

    with pytest.raises(ValueError, match="invalid"):
        security.consume_download_action(first, action[:-1] + ("A" if action[-1] != "A" else "B"))
    with pytest.raises(ValueError, match="another session"):
        security.consume_download_action(second, action)

    payload = security.consume_download_action(first, action)
    assert payload["expected_bytes"] == 4
    with pytest.raises(ValueError, match="already used"):
        security.consume_download_action(first, action)

    expired = security.issue_download_action(
        first,
        url="https://download.kiwix.org/old.zim",
        filename="old.zim",
        expected_bytes=4,
    )
    now[0] += 901
    with pytest.raises(ValueError, match="expired"):
        security.consume_download_action(first, expired)
