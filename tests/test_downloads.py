"""Security and integrity tests for the background ZIM downloader."""

import asyncio
from collections import namedtuple

import httpx
import pytest

from mcp_gateway.downloads import DownloadManager, delete_zim, validate_download_url

GOOD_URL = "https://download.kiwix.org/foo.zim"


class _FakeResponse:
    def __init__(self, status_code: int, headers: dict, body: bytes = b""):
        self.status_code = status_code
        self.headers = headers
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "sensitive upstream detail",
                request=httpx.Request("GET", GOOD_URL),
                response=httpx.Response(self.status_code),
            )

    async def aiter_bytes(self, chunk_size=None):  # noqa: ARG002
        midpoint = max(1, len(self._body) // 2)
        if self._body:
            yield self._body[:midpoint]
            if self._body[midpoint:]:
                yield self._body[midpoint:]


def _factory(respond):
    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, headers=None):
            return respond(method, url, headers)

    return lambda: _Client()


def _manager(tmp_path, respond, **kwargs):
    return DownloadManager(
        tmp_path,
        http_client_factory=_factory(respond),
        validate_fn=kwargs.pop("validate_fn", lambda _path: None),
        min_free_bytes=kwargs.pop("min_free_bytes", 0),
        **kwargs,
    )


def _run(manager: DownloadManager, *, url=GOOD_URL, filename="foo.zim", size=4):
    async def go():
        job = manager.start(url, filename, expected_bytes=size)
        return await manager.wait(job.job_id)

    return asyncio.run(go())


def test_fresh_download_validates_and_installs_atomically(tmp_path):
    body = b"zim!"
    validated = []
    manager = _manager(
        tmp_path,
        lambda _method, _url, _headers: _FakeResponse(
            200, {"content-length": str(len(body))}, body
        ),
        validate_fn=lambda path: validated.append(path.read_bytes()),
    )

    result = _run(manager, size=len(body))

    assert result.status == "done"
    assert (tmp_path / "foo.zim").read_bytes() == body
    assert validated == [body]
    assert not result.staging_path.exists()


@pytest.mark.parametrize(
    "url",
    [
        "http://download.kiwix.org/foo.zim",
        "https://example.org/foo.zim",
        "https://127.0.0.1/foo.zim",
        "https://user:password@download.kiwix.org/foo.zim",
        "https://download.kiwix.org:444/foo.zim",
        "https://download.kiwix.org/foo.zim#fragment",
    ],
)
def test_url_allowlist_rejects_ssrf_targets(url):
    with pytest.raises(ValueError):
        validate_download_url(url)


def test_subdomain_of_official_download_host_is_allowed():
    assert validate_download_url("https://mirror.download.kiwix.org/foo.zim")


def test_start_rejects_unsafe_filename(tmp_path):
    manager = _manager(tmp_path, lambda *_args: None)

    with pytest.raises(ValueError):
        manager.start(GOOD_URL, "../state.db", expected_bytes=4)
    with pytest.raises(ValueError):
        manager.start(GOOD_URL, "foo.txt", expected_bytes=4)

    assert manager.list_jobs() == []


def test_hostile_redirect_is_rejected(tmp_path):
    manager = _manager(
        tmp_path,
        lambda _method, _url, _headers: _FakeResponse(
            302, {"location": "http://127.0.0.1/secrets"}
        ),
    )

    result = _run(manager)

    assert result.status == "error"
    assert "HTTPS" in result.error or "allowed" in result.error
    assert "127.0.0.1" not in result.error


@pytest.mark.parametrize("headers", [{}, {"content-length": "unknown"}])
def test_unknown_response_size_is_rejected(tmp_path, headers):
    manager = _manager(tmp_path, lambda *_args: _FakeResponse(200, headers, b"zim!"))
    assert _run(manager).status == "error"


def test_catalog_and_response_size_must_match(tmp_path):
    manager = _manager(
        tmp_path, lambda *_args: _FakeResponse(200, {"content-length": "5"}, b"zim!!")
    )
    result = _run(manager, size=4)
    assert result.status == "error"
    assert "does not match" in result.error


def test_corrupt_zim_is_not_installed(tmp_path):
    def corrupt(_path):
        raise ValueError("downloaded artifact is not a valid ZIM")

    manager = _manager(
        tmp_path,
        lambda *_args: _FakeResponse(200, {"content-length": "4"}, b"nope"),
        validate_fn=corrupt,
    )
    result = _run(manager)
    assert result.status == "error"
    assert not (tmp_path / "foo.zim").exists()
    assert not result.staging_path.exists()


def test_existing_destination_is_never_replaced(tmp_path):
    (tmp_path / "foo.zim").write_bytes(b"existing")
    manager = _manager(tmp_path, lambda *_args: None)
    with pytest.raises(ValueError, match="already exists"):
        manager.start(GOOD_URL, "foo.zim", expected_bytes=4)
    assert (tmp_path / "foo.zim").read_bytes() == b"existing"


def test_insufficient_disk_space_is_rejected(tmp_path):
    usage = namedtuple("usage", "total used free")(100, 99, 1)
    manager = _manager(tmp_path, lambda *_args: None, disk_usage_fn=lambda _path: usage)
    with pytest.raises(ValueError, match="disk space"):
        manager.start(GOOD_URL, "foo.zim", expected_bytes=4)


def test_http_errors_are_sanitized(tmp_path):
    manager = _manager(tmp_path, lambda *_args: _FakeResponse(500, {}))
    result = _run(manager)
    assert result.status == "error"
    assert result.error == "download server was unreachable or returned an error"
    assert "sensitive" not in result.error


def test_delete_zim_removes_file_and_notifies(tmp_path):
    target = tmp_path / "foo.zim"
    target.write_bytes(b"data")
    completed = []

    assert delete_zim(tmp_path, "foo.zim", on_complete=lambda: completed.append(True)) is True
    assert not target.exists()
    assert completed == [True]


def test_delete_zim_rejects_unsafe_filename(tmp_path):
    outside = tmp_path.parent / "settings.db"
    outside.write_bytes(b"data")

    with pytest.raises(ValueError):
        delete_zim(tmp_path, "../settings.db")
    with pytest.raises(ValueError):
        delete_zim(tmp_path, "settings.db")

    assert outside.exists()


def test_delete_zim_missing_file_returns_false(tmp_path):
    assert delete_zim(tmp_path, "missing.zim") is False
