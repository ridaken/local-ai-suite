"""Security and integrity tests for the background ZIM downloader."""

import asyncio
from collections import namedtuple

import httpx
import pytest

from mcp_gateway.downloads import (
    DownloadJob,
    DownloadJobStore,
    DownloadManager,
    delete_zim,
    validate_download_url,
)

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


def _stored_partial(tmp_path, *, status="paused", body=b"zi", expected=4):
    state_db = tmp_path / "state.db"
    staging = tmp_path / ".staging" / "foo.zim.job1.part"
    staging.parent.mkdir(parents=True)
    staging.write_bytes(body)
    job = DownloadJob(
        job_id="job1",
        url=GOOD_URL,
        filename="foo.zim",
        dest_path=tmp_path / "foo.zim",
        staging_path=staging,
        expected_bytes=expected,
        downloaded_bytes=len(body),
        etag='"v1"',
        status=status,
        created_at=1,
        updated_at=1,
    )
    DownloadJobStore(state_db).create(job)
    return state_db, job


def test_restart_recovers_interrupted_job_as_paused(tmp_path):
    state_db, _job = _stored_partial(tmp_path, status="downloading")

    manager = _manager(tmp_path, lambda *_args: None, state_db=state_db)
    recovered = manager.get_job("job1")

    assert recovered.status == "paused"
    assert recovered.downloaded_bytes == 2
    assert recovered.etag == '"v1"'


def test_resume_requires_matching_content_range_and_installs(tmp_path):
    state_db, _job = _stored_partial(tmp_path)
    seen_headers = []

    def respond(_method, _url, headers):
        seen_headers.append(headers)
        return _FakeResponse(
            206,
            {
                "content-length": "2",
                "content-range": "bytes 2-3/4",
                "etag": '"v1"',
            },
            b"m!",
        )

    manager = _manager(tmp_path, respond, state_db=state_db)

    async def go():
        manager.resume("job1")
        return await manager.wait("job1")

    result = asyncio.run(go())

    assert result.status == "done"
    assert (tmp_path / "foo.zim").read_bytes() == b"zim!"
    assert seen_headers[0]["Range"] == "bytes=2-"
    assert seen_headers[0]["If-Range"] == '"v1"'


def test_resume_rejects_mismatched_range_without_corrupting_partial(tmp_path):
    state_db, job = _stored_partial(tmp_path)
    manager = _manager(
        tmp_path,
        lambda *_args: _FakeResponse(
            206,
            {"content-length": "2", "content-range": "bytes 1-2/4"},
            b"xx",
        ),
        state_db=state_db,
    )

    async def go():
        manager.resume("job1")
        return await manager.wait("job1")

    result = asyncio.run(go())

    assert result.status == "error"
    assert "range" in result.error.lower()
    assert job.staging_path.read_bytes() == b"zi"


def test_retry_discards_partial_and_restarts_from_zero(tmp_path):
    state_db, _job = _stored_partial(tmp_path, status="error")
    seen_headers = []

    def respond(_method, _url, headers):
        seen_headers.append(headers)
        return _FakeResponse(200, {"content-length": "4"}, b"zim!")

    manager = _manager(tmp_path, respond, state_db=state_db)

    async def go():
        manager.retry("job1")
        return await manager.wait("job1")

    result = asyncio.run(go())

    assert result.status == "done"
    assert "Range" not in seen_headers[0]
    assert (tmp_path / "foo.zim").read_bytes() == b"zim!"


def test_graceful_shutdown_persists_active_job_as_paused(tmp_path):
    started = asyncio.Event()
    never = asyncio.Event()

    class SlowResponse(_FakeResponse):
        async def aiter_bytes(self, chunk_size=None):  # noqa: ARG002
            yield b"zi"
            started.set()
            await never.wait()

    manager = _manager(
        tmp_path,
        lambda *_args: SlowResponse(200, {"content-length": "4"}),
    )

    async def go():
        job = manager.start(GOOD_URL, "foo.zim", expected_bytes=4)
        await started.wait()
        await manager.shutdown()
        return manager.get_job(job.job_id)

    result = asyncio.run(go())

    assert result.status == "paused"
    assert result.downloaded_bytes == 2
    assert result.staging_path.read_bytes() == b"zi"


def test_remove_deletes_terminal_record_and_partial(tmp_path):
    state_db, job = _stored_partial(tmp_path, status="cancelled")
    manager = _manager(tmp_path, lambda *_args: None, state_db=state_db)

    manager.remove("job1")

    assert manager.get_job("job1") is None
    assert not job.staging_path.exists()


def test_duplicate_active_destination_is_rejected(tmp_path):
    never = asyncio.Event()

    class SlowResponse(_FakeResponse):
        async def aiter_bytes(self, chunk_size=None):  # noqa: ARG002
            await never.wait()
            yield b"zim!"

    manager = _manager(
        tmp_path,
        lambda *_args: SlowResponse(200, {"content-length": "4"}),
        max_concurrency=2,
    )

    async def go():
        manager.start(GOOD_URL, "foo.zim", expected_bytes=4)
        with pytest.raises(ValueError, match="filename"):
            manager.start(GOOD_URL, "foo.zim", expected_bytes=4)
        await manager.shutdown()

    asyncio.run(go())


def test_user_cancel_keeps_partial_resumable(tmp_path):
    started = asyncio.Event()
    never = asyncio.Event()

    class SlowResponse(_FakeResponse):
        async def aiter_bytes(self, chunk_size=None):  # noqa: ARG002
            yield b"zi"
            started.set()
            await never.wait()

    manager = _manager(
        tmp_path,
        lambda *_args: SlowResponse(200, {"content-length": "4"}),
    )

    async def go():
        job = manager.start(GOOD_URL, "foo.zim", expected_bytes=4)
        await started.wait()
        manager.cancel(job.job_id)
        return await manager.wait(job.job_id)

    result = asyncio.run(go())

    assert result.status == "cancelled"
    assert result.staging_path.read_bytes() == b"zi"


def test_completed_history_is_pruned_to_retention_count(tmp_path):
    state_db = tmp_path / "state.db"
    store = DownloadJobStore(state_db)
    for number in range(3):
        store.create(
            DownloadJob(
                job_id=f"job{number}",
                url=GOOD_URL,
                filename=f"foo{number}.zim",
                dest_path=tmp_path / f"foo{number}.zim",
                staging_path=tmp_path / f"foo{number}.part",
                expected_bytes=4,
                status="done",
                created_at=number,
                updated_at=number,
            )
        )

    manager = _manager(tmp_path, lambda *_args: None, state_db=state_db, retention=2)

    assert [job.job_id for job in manager.list_jobs()] == ["job2", "job1"]
