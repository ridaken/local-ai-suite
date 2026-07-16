"""Constrained background ZIM downloads for the authenticated admin service."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from . import config

_CHUNK_SIZE = 1024 * 1024
_REDIRECT_CODES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 5


def _safe_zim_filename(filename: str) -> str:
    name = Path(filename).name
    if name != filename or not name or name in (".", "..") or not name.lower().endswith(".zim"):
        raise ValueError("filename must be a safe .zim basename")
    return name


def _host_matches(host: str, pattern: str) -> bool:
    if pattern.startswith("*."):
        suffix = pattern[1:]
        return host.endswith(suffix) and host != suffix[1:]
    return host == pattern


def validate_download_url(url: str, allowed_hosts: list[str] | None = None) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("downloads require an HTTPS URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("download URL contains forbidden components")
    if parsed.port not in (None, 443):
        raise ValueError("download URL must use the standard HTTPS port")
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        raise ValueError("download URL may not use an IP literal")
    host = parsed.hostname.lower().rstrip(".")
    patterns = allowed_hosts or config.DOWNLOAD_ALLOWED_HOSTS
    if not any(_host_matches(host, pattern.lower()) for pattern in patterns):
        raise ValueError("download host is not allowed")
    return url


@dataclass
class DownloadJob:
    job_id: str
    url: str
    filename: str
    dest_path: Path
    staging_path: Path
    expected_bytes: int
    status: str = "queued"
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    error: str | None = None


def _default_client() -> httpx.AsyncClient:
    timeout = httpx.Timeout(60.0, connect=10.0, write=10.0, pool=10.0)
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False)


def _validate_zim(path: Path) -> None:
    from libzim.reader import Archive

    archive = Archive(str(path))
    if not str(archive.uuid):
        raise ValueError("downloaded ZIM has no UUID")
    archive.get_metadata("Title")


class DownloadManager:
    def __init__(
        self,
        zim_dir: str | Path,
        *,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
        on_complete: Callable[[], None] | None = None,
        validate_fn: Callable[[Path], None] | None = None,
        disk_usage_fn=shutil.disk_usage,  # noqa: ANN001
        max_bytes: int | None = None,
        min_free_bytes: int | None = None,
        max_concurrency: int | None = None,
        allowed_hosts: list[str] | None = None,
    ) -> None:
        self.zim_dir = Path(zim_dir)
        self._client_factory = http_client_factory or _default_client
        self._on_complete = on_complete
        self._validate_fn = validate_fn or _validate_zim
        self._disk_usage_fn = disk_usage_fn
        self._max_bytes = max_bytes if max_bytes is not None else config.DOWNLOAD_MAX_BYTES
        self._min_free_bytes = (
            min_free_bytes if min_free_bytes is not None else config.DOWNLOAD_MIN_FREE_BYTES
        )
        self._max_concurrency = (
            max_concurrency if max_concurrency is not None else config.DOWNLOAD_MAX_CONCURRENCY
        )
        self._allowed_hosts = allowed_hosts or config.DOWNLOAD_ALLOWED_HOSTS
        self._jobs: dict[str, DownloadJob] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._active_filenames: set[str] = set()

    def list_jobs(self) -> list[DownloadJob]:
        return list(self._jobs.values())

    def get_job(self, job_id: str) -> DownloadJob | None:
        return self._jobs.get(job_id)

    def start(self, url: str, filename: str, *, expected_bytes: int) -> DownloadJob:
        safe_filename = _safe_zim_filename(filename)
        validate_download_url(url, self._allowed_hosts)
        if expected_bytes <= 0:
            raise ValueError("catalog size is required")
        if expected_bytes > self._max_bytes:
            raise ValueError("download exceeds configured maximum size")
        if safe_filename in self._active_filenames:
            raise ValueError("a download for this filename is already active")
        if len(self._active_filenames) >= self._max_concurrency:
            raise ValueError("maximum concurrent downloads reached")
        self.zim_dir.mkdir(parents=True, exist_ok=True)
        dest = self.zim_dir / safe_filename
        if dest.exists():
            raise ValueError("destination already exists")
        required_free = expected_bytes + max(self._min_free_bytes, expected_bytes // 20)
        if self._disk_usage_fn(self.zim_dir).free < required_free:
            raise ValueError("insufficient free disk space")
        job_id = uuid.uuid4().hex[:12]
        staging = self.zim_dir / ".staging" / f"{safe_filename}.{job_id}.part"
        job = DownloadJob(job_id, url, safe_filename, dest, staging, expected_bytes)
        self._jobs[job_id] = job
        self._active_filenames.add(safe_filename)
        self._tasks[job_id] = asyncio.create_task(self._run(job))
        return job

    async def wait(self, job_id: str) -> DownloadJob:
        task = self._tasks.get(job_id)
        if task is not None:
            await task
        return self._jobs[job_id]

    def cancel(self, job_id: str) -> None:
        task = self._tasks.get(job_id)
        if task is not None:
            task.cancel()

    async def _run(self, job: DownloadJob) -> None:
        job.staging_path.parent.mkdir(parents=True, exist_ok=True)
        headers = {"User-Agent": config.USER_AGENT}
        job.status = "downloading"
        current_url = job.url
        try:
            async with self._client_factory() as client:
                for redirect_count in range(_MAX_REDIRECTS + 1):
                    async with client.stream("GET", current_url, headers=headers) as resp:
                        if resp.status_code in _REDIRECT_CODES:
                            if redirect_count >= _MAX_REDIRECTS:
                                raise ValueError("too many redirects")
                            location = resp.headers.get("location", "")
                            current_url = validate_download_url(
                                urljoin(current_url, location), self._allowed_hosts
                            )
                            continue
                        resp.raise_for_status()
                        if resp.status_code != 200:
                            raise ValueError("download server returned an unsupported response")
                        content_length = resp.headers.get("content-length")
                        if content_length is None or not content_length.isdigit():
                            raise ValueError("download response did not include a valid size")
                        total = int(content_length)
                        if total != job.expected_bytes:
                            raise ValueError("download size does not match the catalog")
                        job.total_bytes = total
                        async for chunk in resp.aiter_bytes(chunk_size=_CHUNK_SIZE):
                            await asyncio.to_thread(_append_chunk, job.staging_path, chunk)
                            job.downloaded_bytes += len(chunk)
                            if job.downloaded_bytes > job.expected_bytes:
                                raise ValueError("download exceeded the expected size")
                        break
                else:  # pragma: no cover - loop exits by break or exception
                    raise ValueError("too many redirects")
            if job.downloaded_bytes != job.expected_bytes:
                raise ValueError("download ended before the expected size")
            await asyncio.to_thread(self._validate_fn, job.staging_path)
            await asyncio.to_thread(os.replace, job.staging_path, job.dest_path)
            job.status = "done"
            if self._on_complete:
                await asyncio.to_thread(self._on_complete)
        except asyncio.CancelledError:
            job.status = "cancelled"
            await asyncio.to_thread(job.staging_path.unlink, missing_ok=True)
            raise
        except Exception as exc:  # noqa: BLE001 - sanitized status is shown in the admin UI
            job.status = "error"
            job.error = _public_error(exc)
            await asyncio.to_thread(job.staging_path.unlink, missing_ok=True)
        finally:
            self._active_filenames.discard(job.filename)


def _public_error(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        return str(exc)
    if isinstance(exc, httpx.HTTPError):
        return "download server was unreachable or returned an error"
    return "download validation failed"


def _append_chunk(path: Path, chunk: bytes) -> None:
    with path.open("ab") as file:
        file.write(chunk)


def delete_zim(
    zim_dir: str | Path, filename: str, *, on_complete: Callable[[], None] | None = None
) -> bool:
    path = Path(zim_dir) / _safe_zim_filename(filename)
    if not path.exists():
        return False
    path.unlink()
    if on_complete:
        on_complete()
    return True
