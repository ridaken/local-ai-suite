"""Tests for the server-rendered admin UI, driven with Starlette's TestClient.
External calls (service reachability checks, the OPDS catalog, ZIM metadata
reads) are monkeypatched so these run offline and deterministically."""

import httpx
from starlette.testclient import TestClient

from mcp_gateway import admin
from mcp_gateway.catalog import CatalogEntry
from mcp_gateway.downloads import DownloadManager
from mcp_gateway.settings_store import SettingsStore
from mcp_gateway.zim_library import BookInfo


def _client(tmp_path, *, download_manager=None):
    zim_dir = tmp_path / "zim"
    zim_dir.mkdir()
    settings = SettingsStore(tmp_path / "settings.db")
    manager = download_manager or DownloadManager(zim_dir)
    app = admin.build_admin_app(
        settings=settings,
        download_manager=manager,
        zim_dir=str(zim_dir),
        library_xml_path=str(zim_dir / "library.xml"),
    )
    return TestClient(app), settings, manager, zim_dir


async def _fake_reachable(url: str) -> str:
    return "reachable" if url else "unconfigured"


def test_dashboard_renders_service_badges(tmp_path, monkeypatch):
    client, _settings, _mgr, _zim_dir = _client(tmp_path)
    monkeypatch.setattr(admin, "_reachable", _fake_reachable)

    resp = client.get("/")
    assert resp.status_code == 200
    assert "reachable" in resp.text
    assert "Dashboard" in resp.text or "local-ai-suite admin" in resp.text


def test_sources_lists_installed_books_with_toggle_state(tmp_path, monkeypatch):
    client, settings, _mgr, _zim_dir = _client(tmp_path)
    books = [
        BookInfo(filename="a.zim", name="book_a", uuid="u1", title="Book A", article_count=10),
        BookInfo(filename="b.zim", name="book_b", uuid="u2", title="Book B", article_count=20),
    ]
    monkeypatch.setattr(admin.zim_library, "scan_zim_dir", lambda _dir: books)
    settings.set_book_enabled("book_b", False)

    resp = client.get("/sources")
    assert resp.status_code == 200
    assert "Book A" in resp.text
    assert "Book B" in resp.text
    assert "disabled" in resp.text  # book_b


def test_sources_toggle_flips_setting(tmp_path, monkeypatch):
    client, settings, _mgr, _zim_dir = _client(tmp_path)
    monkeypatch.setattr(admin.zim_library, "scan_zim_dir", lambda _dir: [])

    assert settings.is_book_enabled("book_x") is True
    resp = client.post(
        "/sources/toggle", data={"name": "book_x", "enabled": "0"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/sources"
    assert settings.is_book_enabled("book_x") is False


def test_sources_delete_removes_file_and_writes_library_xml(tmp_path, monkeypatch):
    client, _settings, _mgr, zim_dir = _client(tmp_path)
    (zim_dir / "gone.zim").write_bytes(b"data")
    monkeypatch.setattr(admin.zim_library, "scan_zim_dir", lambda _dir: [])

    resp = client.post("/sources/delete", data={"filename": "gone.zim"}, follow_redirects=False)
    assert resp.status_code == 303
    assert not (zim_dir / "gone.zim").exists()
    assert (zim_dir / "library.xml").exists()


def test_catalog_page_shows_download_button_for_available_entry(tmp_path, monkeypatch):
    client, _settings, _mgr, _zim_dir = _client(tmp_path)

    async def fake_search(query="", lang="", count=30):
        return [
            CatalogEntry(
                uuid="u1",
                name="devdocs_python",
                title="Python Docs",
                description="",
                language="eng",
                category="other",
                tags="_ftindex:yes",
                article_count=100,
                media_count=0,
                updated="",
                size_bytes=1024,
                download_url="https://example.org/devdocs_python.zim",
                has_fulltext_index=True,
            )
        ]

    monkeypatch.setattr(admin.catalog_client, "search_catalog", fake_search)
    resp = client.get("/catalog", params={"q": "python"})
    assert resp.status_code == 200
    assert "Python Docs" in resp.text
    assert "Download" in resp.text
    assert "full-text index" in resp.text


def test_catalog_page_handles_unreachable_catalog(tmp_path, monkeypatch):
    client, _settings, _mgr, _zim_dir = _client(tmp_path)

    async def fake_search(query="", lang="", count=30):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(admin.catalog_client, "search_catalog", fake_search)
    resp = client.get("/catalog", params={"q": "python"})
    assert resp.status_code == 200
    assert "unreachable" in resp.text.lower() or "ConnectError" in resp.text


def test_sources_download_registers_a_job(tmp_path):
    class _StuckClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, headers=None):
            raise AssertionError("not reached in this test")

    manager = DownloadManager(tmp_path / "zim", http_client_factory=lambda: _StuckClient())
    client, _settings, mgr, _zd = _client(tmp_path, download_manager=manager)

    resp = client.post(
        "/sources/download",
        data={"url": "https://example.org/foo.zim", "filename": "foo.zim"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/downloads"
    jobs = mgr.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].filename == "foo.zim"
    assert jobs[0].url == "https://example.org/foo.zim"


def test_settings_page_reflects_current_values(tmp_path):
    client, settings, _mgr, _zim_dir = _client(tmp_path)
    settings.set_retrieval_mode("lexical")
    settings.set_rerank_enabled(False)

    resp = client.get("/settings")
    assert resp.status_code == 200
    assert 'value="lexical" checked' in resp.text


def test_settings_update_changes_mode_and_rerank(tmp_path):
    client, settings, _mgr, _zim_dir = _client(tmp_path)

    resp = client.post(
        "/settings/update", data={"mode": "vector"}, follow_redirects=False
    )  # rerank checkbox omitted == unchecked
    assert resp.status_code == 303
    assert settings.get_retrieval_mode() == "vector"
    assert settings.get_rerank_enabled() is False
