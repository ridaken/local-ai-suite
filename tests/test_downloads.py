"""Tests for the resumable background download manager, against a fake httpx
client (no network) so resume-from-partial-file and error paths are
deterministic."""

import asyncio

import httpx
import pytest

from mcp_gateway.downloads import DownloadManager, delete_zim


class _FakeResponse:
    def __init__(self, status_code: int, headers: dict, body: bytes):
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
                "error",
                request=httpx.Request("GET", "http://x"),
                response=httpx.Response(self.status_code),
            )

    async def aiter_bytes(self, chunk_size=None):
        # split into two chunks (when possible) to exercise multi-write accumulation
        if not self._body:
            return
        mid = max(1, len(self._body) // 2)
        yield self._body[:mid]
        if self._body[mid:]:
            yield self._body[mid:]


def _factory(respond):
    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, headers=None):
            return respond(headers)

    return lambda: _Client()


def _run_to_completion(manager: DownloadManager, url: str, filename: str):
    """start() calls asyncio.create_task, so it needs a running loop — same
    requirement production has (called from an async admin route)."""

    async def _go():
        job = manager.start(url, filename)
        return await manager.wait(job.job_id)

    return asyncio.run(_go())


def test_fresh_download_writes_full_content(tmp_path):
    body = b"0123456789" * 10
    calls = []

    def respond(headers):
        calls.append(headers)
        return _FakeResponse(200, {"content-length": str(len(body))}, body)

    completed = []
    manager = DownloadManager(
        tmp_path, http_client_factory=_factory(respond), on_complete=lambda: completed.append(True)
    )

    result = _run_to_completion(manager, "http://example/foo.zim", "foo.zim")

    assert result.status == "done"
    assert result.downloaded_bytes == len(body)
    assert result.total_bytes == len(body)
    assert (tmp_path / "foo.zim").read_bytes() == body
    assert not (tmp_path / "foo.zim.part").exists()
    assert completed == [True]
    assert "Range" not in calls[0]


def test_start_rejects_path_traversal_filename(tmp_path):
    manager = DownloadManager(tmp_path, http_client_factory=_factory(lambda headers: None))

    with pytest.raises(ValueError):
        manager.start("http://example/foo.zim", "../state.db")

    assert manager.list_jobs() == []
    assert not (tmp_path.parent / "state.db").exists()


def test_start_rejects_non_zim_filename(tmp_path):
    manager = DownloadManager(tmp_path, http_client_factory=_factory(lambda headers: None))

    with pytest.raises(ValueError):
        manager.start("http://example/foo.txt", "foo.txt")

    assert manager.list_jobs() == []


def test_resume_continues_from_partial_file(tmp_path):
    first_half = b"AAAA"
    second_half = b"BBBB"
    full = first_half + second_half
    (tmp_path / "foo.zim.part").write_bytes(first_half)

    seen_headers = []

    def respond(headers):
        seen_headers.append(headers)
        return _FakeResponse(206, {"content-length": str(len(second_half))}, second_half)

    manager = DownloadManager(tmp_path, http_client_factory=_factory(respond))
    result = _run_to_completion(manager, "http://example/foo.zim", "foo.zim")

    assert result.status == "done"
    assert (tmp_path / "foo.zim").read_bytes() == full
    assert result.downloaded_bytes == len(full)
    assert result.total_bytes == len(full)
    assert seen_headers[0]["Range"] == f"bytes={len(first_half)}-"


def test_download_error_sets_status_and_message(tmp_path):
    def respond(headers):
        return _FakeResponse(500, {}, b"")

    manager = DownloadManager(tmp_path, http_client_factory=_factory(respond))
    result = _run_to_completion(manager, "http://example/bad.zim", "bad.zim")

    assert result.status == "error"
    assert "HTTPStatusError" in result.error


def test_delete_zim_removes_file_and_notifies(tmp_path):
    target = tmp_path / "foo.zim"
    target.write_bytes(b"data")
    completed = []

    assert delete_zim(tmp_path, "foo.zim", on_complete=lambda: completed.append(True)) is True
    assert not target.exists()
    assert completed == [True]


def test_delete_zim_rejects_path_traversal_filename(tmp_path):
    outside = tmp_path.parent / "settings.db"
    outside.write_bytes(b"data")

    with pytest.raises(ValueError):
        delete_zim(tmp_path, "../settings.db")

    assert outside.exists()


def test_delete_zim_rejects_non_zim_filename(tmp_path):
    target = tmp_path / "settings.db"
    target.write_bytes(b"data")

    with pytest.raises(ValueError):
        delete_zim(tmp_path, "settings.db")

    assert target.exists()


def test_delete_zim_missing_file_returns_false(tmp_path):
    assert delete_zim(tmp_path, "missing.zim") is False
