"""Durable, constrained background ZIM downloads for the admin service."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import os
import re
import shutil
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from urllib.parse import urljoin, urlsplit

import httpx

from . import config

_CHUNK_SIZE = 1024 * 1024
_REDIRECT_CODES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 5
_ACTIVE_STATUSES = ("queued", "downloading")
_RESUMABLE_STATUSES = ("paused", "cancelled", "error")
_TERMINAL_STATUSES = ("done", "cancelled", "error")
_CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")
_DOWNLOAD_SCHEMA_VERSION = 1


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
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None
    created_at: int = 0
    updated_at: int = 0


class DownloadJobStore:
    """Versioned SQLite persistence for download state."""

    def __init__(self, db_path: str | Path, *, now_fn=time.time) -> None:
        self.db_path = str(db_path)
        self._now_fn = now_fn
        self._lock = Lock()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS download_schema (
                    name TEXT PRIMARY KEY,
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS download_jobs (
                    job_id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    expected_bytes INTEGER NOT NULL,
                    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                    total_bytes INTEGER,
                    etag TEXT,
                    last_modified TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    staging_path TEXT NOT NULL,
                    dest_path TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS download_jobs_active_filename
                    ON download_jobs(filename)
                    WHERE status IN ('queued', 'downloading');
                """
            )
            row = conn.execute(
                "SELECT version FROM download_schema WHERE name = 'download_jobs'"
            ).fetchone()
            if row and int(row[0]) > _DOWNLOAD_SCHEMA_VERSION:
                raise RuntimeError("download state was created by a newer application version")
            conn.execute(
                "INSERT OR REPLACE INTO download_schema(name, version) VALUES (?, ?)",
                ("download_jobs", _DOWNLOAD_SCHEMA_VERSION),
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def recover_interrupted(self) -> None:
        with self._lock, contextlib.closing(self._connect()) as conn:
            conn.execute(
                "UPDATE download_jobs SET status='paused', error=NULL, updated_at=? "
                "WHERE status IN ('queued', 'downloading')",
                (int(self._now_fn()),),
            )
            conn.commit()

    def create(self, job: DownloadJob) -> None:
        with self._lock, contextlib.closing(self._connect()) as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO download_jobs(
                        job_id, url, filename, expected_bytes, downloaded_bytes,
                        total_bytes, etag, last_modified, status, error,
                        created_at, updated_at, staging_path, dest_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job.job_id, job.url, job.filename, job.expected_bytes,
                        job.downloaded_bytes, job.total_bytes, job.etag,
                        job.last_modified, job.status, job.error, job.created_at,
                        job.updated_at, str(job.staging_path), str(job.dest_path),
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError("a download for this filename is already active") from exc

    def save(self, job: DownloadJob) -> None:
        job.updated_at = int(self._now_fn())
        with self._lock, contextlib.closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE download_jobs SET downloaded_bytes=?, total_bytes=?, etag=?,
                    last_modified=?, status=?, error=?, updated_at=? WHERE job_id=?
                """,
                (
                    job.downloaded_bytes, job.total_bytes, job.etag,
                    job.last_modified, job.status, job.error, job.updated_at,
                    job.job_id,
                ),
            )
            conn.commit()

    def list(self) -> list[DownloadJob]:
        with self._lock, contextlib.closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM download_jobs ORDER BY created_at DESC, job_id DESC"
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    def get(self, job_id: str) -> DownloadJob | None:
        with self._lock, contextlib.closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM download_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        return _row_to_job(row) if row else None

    def remove(self, job_id: str) -> None:
        with self._lock, contextlib.closing(self._connect()) as conn:
            conn.execute("DELETE FROM download_jobs WHERE job_id=?", (job_id,))
            conn.commit()

    def prune(self, retention: int) -> None:
        with self._lock, contextlib.closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT job_id FROM download_jobs WHERE status = 'done' "
                "ORDER BY updated_at DESC, job_id DESC LIMIT -1 OFFSET ?",
                (retention,),
            ).fetchall()
            conn.executemany(
                "DELETE FROM download_jobs WHERE job_id=?", [(row[0],) for row in rows]
            )
            conn.commit()


def _row_to_job(row: sqlite3.Row) -> DownloadJob:
    return DownloadJob(
        job_id=row["job_id"],
        url=row["url"],
        filename=row["filename"],
        dest_path=Path(row["dest_path"]),
        staging_path=Path(row["staging_path"]),
        expected_bytes=int(row["expected_bytes"]),
        status=row["status"],
        downloaded_bytes=int(row["downloaded_bytes"]),
        total_bytes=row["total_bytes"],
        etag=row["etag"],
        last_modified=row["last_modified"],
        error=row["error"],
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


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
        state_db: str | Path | None = None,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
        on_complete: Callable[[], None] | None = None,
        validate_fn: Callable[[Path], None] | None = None,
        disk_usage_fn=shutil.disk_usage,  # noqa: ANN001
        max_bytes: int | None = None,
        min_free_bytes: int | None = None,
        max_concurrency: int | None = None,
        retention: int | None = None,
        allowed_hosts: list[str] | None = None,
        now_fn=time.time,
    ) -> None:
        self.zim_dir = Path(zim_dir)
        state_db = state_db or self.zim_dir.parent / f".{self.zim_dir.name}-download-jobs.db"
        self.store = DownloadJobStore(state_db, now_fn=now_fn)
        self.store.recover_interrupted()
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
        self._retention = retention if retention is not None else config.DOWNLOAD_HISTORY_RETENTION
        self._allowed_hosts = allowed_hosts or config.DOWNLOAD_ALLOWED_HOSTS
        self._tasks: dict[str, asyncio.Task] = {}
        self._active_filenames: set[str] = set()
        self._user_cancellations: set[str] = set()
        self._shutting_down = False
        self._reconcile_partials()
        self.store.prune(self._retention)

    def _reconcile_partials(self) -> None:
        for job in self.store.list():
            if job.status != "paused":
                continue
            try:
                size = job.staging_path.stat().st_size
            except OSError:
                size = 0
            job.downloaded_bytes = min(size, job.expected_bytes)
            if size > job.expected_bytes:
                job.status = "error"
                job.error = "staged download exceeds the expected size"
            self.store.save(job)

    def list_jobs(self) -> list[DownloadJob]:
        return self.store.list()

    def get_job(self, job_id: str) -> DownloadJob | None:
        return self.store.get(job_id)

    def _validate_capacity(self, job: DownloadJob, *, remaining: int) -> None:
        if job.filename in self._active_filenames:
            raise ValueError("a download for this filename is already active")
        if len(self._active_filenames) >= self._max_concurrency:
            raise ValueError("maximum concurrent downloads reached")
        if job.dest_path.exists():
            raise ValueError("destination already exists")
        required_free = remaining + max(self._min_free_bytes, job.expected_bytes // 20)
        if self._disk_usage_fn(self.zim_dir).free < required_free:
            raise ValueError("insufficient free disk space")

    def start(self, url: str, filename: str, *, expected_bytes: int) -> DownloadJob:
        if self._shutting_down:
            raise ValueError("download manager is shutting down")
        safe_filename = _safe_zim_filename(filename)
        validate_download_url(url, self._allowed_hosts)
        if expected_bytes <= 0:
            raise ValueError("catalog size is required")
        if expected_bytes > self._max_bytes:
            raise ValueError("download exceeds configured maximum size")
        self.zim_dir.mkdir(parents=True, exist_ok=True)
        now = int(time.time())
        job_id = uuid.uuid4().hex[:12]
        job = DownloadJob(
            job_id=job_id,
            url=url,
            filename=safe_filename,
            dest_path=self.zim_dir / safe_filename,
            staging_path=self.zim_dir / ".staging" / f"{safe_filename}.{job_id}.part",
            expected_bytes=expected_bytes,
            created_at=now,
            updated_at=now,
        )
        self._validate_capacity(job, remaining=expected_bytes)
        self.store.create(job)
        self._launch(job)
        return job

    def resume(self, job_id: str) -> DownloadJob:
        job = self._require_job(job_id)
        if job.status not in _RESUMABLE_STATUSES:
            raise ValueError("download is not resumable")
        validate_download_url(job.url, self._allowed_hosts)
        try:
            actual = job.staging_path.stat().st_size
        except OSError:
            actual = 0
        if actual <= 0 or actual >= job.expected_bytes:
            raise ValueError("download has no valid partial file to resume")
        job.downloaded_bytes = actual
        job.error = None
        job.status = "queued"
        self._validate_capacity(job, remaining=job.expected_bytes - actual)
        self.store.save(job)
        self._launch(job)
        return job

    def retry(self, job_id: str) -> DownloadJob:
        job = self._require_job(job_id)
        if job.status not in _RESUMABLE_STATUSES:
            raise ValueError("download is not retryable")
        self._validate_capacity(job, remaining=job.expected_bytes)
        job.staging_path.unlink(missing_ok=True)
        job.downloaded_bytes = 0
        job.total_bytes = None
        job.etag = None
        job.last_modified = None
        job.error = None
        job.status = "queued"
        self.store.save(job)
        self._launch(job)
        return job

    async def wait(self, job_id: str) -> DownloadJob:
        task = self._tasks.get(job_id)
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return self._require_job(job_id)

    def cancel(self, job_id: str) -> None:
        job = self._require_job(job_id)
        task = self._tasks.get(job_id)
        if job.status not in _ACTIVE_STATUSES or task is None:
            raise ValueError("download is not active")
        self._user_cancellations.add(job_id)
        if not task.cancel():
            self._user_cancellations.discard(job_id)
            raise ValueError("download is not active")
        job.status = "cancelled"
        job.error = None
        self.store.save(job)

    def remove(self, job_id: str) -> None:
        job = self._require_job(job_id)
        if job.status not in _TERMINAL_STATUSES and job.status != "paused":
            raise ValueError("active downloads cannot be removed")
        job.staging_path.unlink(missing_ok=True)
        self.store.remove(job_id)

    async def shutdown(self) -> None:
        self._shutting_down = True
        pending = [(job_id, task) for job_id, task in self._tasks.items() if not task.done()]
        for _job_id, task in pending:
            task.cancel()
        tasks = [task for _job_id, task in pending]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for job_id, _task in pending:
            job = self.store.get(job_id)
            if job is not None and job.status in _ACTIVE_STATUSES:
                job.status = "paused"
                job.error = None
                try:
                    job.downloaded_bytes = min(
                        job.staging_path.stat().st_size, job.expected_bytes
                    )
                except OSError:
                    job.downloaded_bytes = 0
                self.store.save(job)

    def _require_job(self, job_id: str) -> DownloadJob:
        job = self.store.get(job_id)
        if job is None:
            raise ValueError("unknown download job")
        return job

    def _launch(self, job: DownloadJob) -> None:
        if self._shutting_down:
            raise ValueError("download manager is shutting down")
        self._active_filenames.add(job.filename)
        self._tasks[job.job_id] = asyncio.create_task(self._run(job))

    async def _run(self, job: DownloadJob) -> None:
        job.staging_path.parent.mkdir(parents=True, exist_ok=True)
        resume_at = job.downloaded_bytes
        headers = {"User-Agent": config.USER_AGENT}
        if resume_at:
            headers["Range"] = f"bytes={resume_at}-"
            validator = job.etag or job.last_modified
            if validator:
                headers["If-Range"] = validator
        job.status = "downloading"
        self.store.save(job)
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
                        self._validate_response(job, resp, resume_at)
                        if not resume_at:
                            job.etag = resp.headers.get("etag")
                            job.last_modified = resp.headers.get("last-modified")
                        job.total_bytes = job.expected_bytes
                        self.store.save(job)
                        async for chunk in resp.aiter_bytes(chunk_size=_CHUNK_SIZE):
                            await asyncio.to_thread(_append_chunk, job.staging_path, chunk)
                            job.downloaded_bytes += len(chunk)
                            if job.downloaded_bytes > job.expected_bytes:
                                raise ValueError("download exceeded the expected size")
                            self.store.save(job)
                        break
                else:  # pragma: no cover
                    raise ValueError("too many redirects")
            if job.downloaded_bytes != job.expected_bytes:
                raise ValueError("download ended before the expected size")
            finish = asyncio.create_task(self._finish_install(job))
            try:
                await asyncio.shield(finish)
            except asyncio.CancelledError:
                # Validation/install is the non-interruptible commit point. Await
                # it so a background to_thread call cannot install a ZIM after
                # the job has already been persisted as paused/cancelled.
                await finish
            job.status = "done"
            job.error = None
            self.store.save(job)
            if self._on_complete:
                await asyncio.to_thread(self._on_complete)
        except asyncio.CancelledError:
            job.status = (
                "cancelled" if job.job_id in self._user_cancellations else "paused"
            )
            job.error = None
            self.store.save(job)
            raise
        except Exception as exc:  # noqa: BLE001 - only sanitized text leaves this boundary
            job.status = "error"
            job.error = _public_error(exc)
            if job.downloaded_bytes >= job.expected_bytes:
                job.staging_path.unlink(missing_ok=True)
                job.downloaded_bytes = 0
            self.store.save(job)
        finally:
            self._active_filenames.discard(job.filename)
            self._user_cancellations.discard(job.job_id)
            self.store.prune(self._retention)

    async def _finish_install(self, job: DownloadJob) -> None:
        await asyncio.to_thread(self._validate_fn, job.staging_path)
        await asyncio.to_thread(_install_no_replace, job.staging_path, job.dest_path)

    def _validate_response(self, job: DownloadJob, resp, resume_at: int) -> None:  # noqa: ANN001
        length = resp.headers.get("content-length", "")
        if not length.isdigit():
            raise ValueError("download response did not include a valid size")
        if resume_at:
            if resp.status_code != 206:
                raise ValueError("download server did not honor the resume request")
            match = _CONTENT_RANGE_RE.fullmatch(resp.headers.get("content-range", ""))
            if not match:
                raise ValueError("download response had an invalid Content-Range")
            start, end, total = (int(value) for value in match.groups())
            if start != resume_at or total != job.expected_bytes or end < start:
                raise ValueError("download response range does not match the partial file")
            if int(length) != end - start + 1:
                raise ValueError("download response length does not match its range")
            if job.etag and resp.headers.get("etag") not in (None, job.etag):
                raise ValueError("download validator changed; retry from the beginning")
            if job.last_modified and resp.headers.get("last-modified") not in (
                None, job.last_modified
            ):
                raise ValueError("download validator changed; retry from the beginning")
        else:
            if resp.status_code != 200:
                raise ValueError("download server returned an unsupported response")
            if int(length) != job.expected_bytes:
                raise ValueError("download size does not match the catalog")


def _public_error(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        return str(exc)
    if isinstance(exc, httpx.HTTPError):
        return "download server was unreachable or returned an error"
    return "download validation failed"


def _append_chunk(path: Path, chunk: bytes) -> None:
    with path.open("ab") as file:
        file.write(chunk)


def _install_no_replace(staging: Path, destination: Path) -> None:
    """Atomically install on the same filesystem without replacing a destination."""
    try:
        os.link(staging, destination)
    except FileExistsError as exc:
        raise ValueError("destination already exists") from exc
    staging.unlink()


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
