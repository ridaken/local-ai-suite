"""Background, resumable ZIM downloads for the admin UI.

Downloads run as asyncio tasks so the admin UI can start one and poll progress
without blocking the request. Partial downloads land in a `.part` file next to
the destination; a restart resumes via an HTTP Range request instead of
starting over — the natural failure mode for a 75GB StackOverflow ZIM on a
home connection.

`http_client_factory` is injectable so tests can exercise resume/error/cancel
paths against a fake client instead of the network (same pattern as
retrieval/hybrid.py's injectable embed_fn/rerank_fn/client).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import config

_CHUNK_SIZE = 1024 * 1024


@dataclass
class DownloadJob:
    job_id: str
    url: str
    filename: str
    dest_path: Path
    status: str = "queued"  # queued | downloading | done | error | cancelled
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    error: str | None = None


def _default_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=None, follow_redirects=True)


class DownloadManager:
    def __init__(
        self,
        zim_dir: str | Path,
        *,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
        on_complete: Callable[[], None] | None = None,
    ):
        self.zim_dir = Path(zim_dir)
        self._client_factory = http_client_factory or _default_client
        self._on_complete = on_complete
        self._jobs: dict[str, DownloadJob] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def list_jobs(self) -> list[DownloadJob]:
        return list(self._jobs.values())

    def get_job(self, job_id: str) -> DownloadJob | None:
        return self._jobs.get(job_id)

    def start(self, url: str, filename: str) -> DownloadJob:
        job = DownloadJob(
            job_id=uuid.uuid4().hex[:12],
            url=url,
            filename=filename,
            dest_path=self.zim_dir / filename,
        )
        self._jobs[job.job_id] = job
        self._tasks[job.job_id] = asyncio.create_task(self._run(job))
        return job

    async def wait(self, job_id: str) -> DownloadJob:
        """Await a job's task to completion. Mainly for tests/CLI use — the
        admin UI polls get_job() instead of blocking a request on this."""
        task = self._tasks.get(job_id)
        if task is not None:
            await task
        return self._jobs[job_id]

    def cancel(self, job_id: str) -> None:
        task = self._tasks.get(job_id)
        if task is not None:
            task.cancel()

    async def _run(self, job: DownloadJob) -> None:
        self.zim_dir.mkdir(parents=True, exist_ok=True)
        part_path = job.dest_path.with_name(job.dest_path.name + ".part")
        resume_from = part_path.stat().st_size if part_path.exists() else 0
        headers = {"User-Agent": config.USER_AGENT}
        if resume_from:
            headers["Range"] = f"bytes={resume_from}-"

        job.status = "downloading"
        job.downloaded_bytes = resume_from
        try:
            async with self._client_factory() as client, client.stream(
                "GET", job.url, headers=headers
            ) as resp:
                resp.raise_for_status()
                resumed = resume_from and resp.status_code == 206
                content_length = resp.headers.get("content-length")
                if content_length is not None:
                    job.total_bytes = int(content_length) + (resume_from if resumed else 0)

                mode = "ab" if resumed else "wb"
                if mode == "wb":
                    resume_from = 0
                    job.downloaded_bytes = 0

                with open(part_path, mode) as f:
                    async for chunk in resp.aiter_bytes(chunk_size=_CHUNK_SIZE):
                        f.write(chunk)
                        job.downloaded_bytes += len(chunk)

            os.replace(part_path, job.dest_path)
            job.status = "done"
            if self._on_complete:
                self._on_complete()
        except asyncio.CancelledError:
            job.status = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to the admin UI, not raised
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"


def delete_zim(
    zim_dir: str | Path, filename: str, *, on_complete: Callable[[], None] | None = None
) -> bool:
    """Delete an installed ZIM. Returns False if it wasn't there."""
    path = Path(zim_dir) / filename
    if not path.exists():
        return False
    path.unlink()
    if on_complete:
        on_complete()
    return True
