"""Incremental ingest driver.

Walks the sources declared in sources.yaml, hashes each file, and re-embeds only
what changed since last run (tracked in a sqlite manifest). This is what makes
"re-index on save" cheap — embedding, the only costly step, touches changed files
only.

Recoverability is the design constraint. Ingest talks to two stores that cannot
be updated atomically together (Qdrant and the manifest), and it can die halfway
through — Ctrl-C, an embedder timeout, a full disk. So each file is committed as
its own unit, in an order chosen so that every possible interruption point leaves
a searchable index: new chunks are upserted before stale ones are deleted, and
the manifest row is written last. A crash can therefore leave duplicate vectors
(harmless — retrieval dedups, and the next run cleans them up) but never a file
that is indexed as current while its vectors are missing.

Run:  python -m ingest.ingest
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from mcp_gateway import config
from retrieval.embed import embed_texts
from retrieval.qdrant_store import delete_by_chunk_ids, ensure_collection, get_client, upsert_chunks

from .chunking import chunk_file

EmbedTextsFn = Callable[[list[str]], Awaitable[list[list[float]]]]

_DEFAULT_EXCLUDE_DIRS = {".git", ".venv", "node_modules", "__pycache__"}
_MAX_FILE_BYTES = 1_000_000

# Manifest statuses.
STATUS_INDEXED = "indexed"
STATUS_SKIPPED = "skipped"  # intentionally not indexable — vectors removed
STATUS_ERROR = "error"  # transient failure — last good version retained


class SourceConfigError(ValueError):
    """sources.yaml is invalid. Raised before any store is touched."""


@dataclass
class Source:
    root: Path
    label: str
    id: str = ""
    include: list[str] = field(default_factory=lambda: ["**/*"])
    exclude: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = self.label


def load_sources(sources_path: Path) -> list[Source]:
    """Parse and validate sources.yaml.

    Every source needs a stable, unique `id`: it is what lets a source be
    renamed, re-labelled, or moved without orphaning its vectors. Labels must be
    unique too — they prefix the display path that becomes the manifest key and
    the citation, so two sources sharing a label would silently overwrite each
    other's manifest rows.
    """
    raw = yaml.safe_load(sources_path.read_text(encoding="utf-8")) or {}
    entries = raw.get("sources", [])
    if not isinstance(entries, list):
        raise SourceConfigError("sources.yaml: 'sources' must be a list")

    sources: list[Source] = []
    seen_ids: set[str] = set()
    seen_labels: set[str] = set()
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise SourceConfigError(f"sources.yaml: source #{position} must be a mapping")
        source_id = str(entry.get("id", "")).strip()
        if not source_id:
            raise SourceConfigError(
                f"sources.yaml: source #{position} is missing a stable 'id' "
                "(add a short unique name, e.g. id: my-notes)"
            )
        if source_id in seen_ids:
            raise SourceConfigError(f"sources.yaml: duplicate source id {source_id!r}")
        if "root" not in entry:
            raise SourceConfigError(f"sources.yaml: source {source_id!r} is missing 'root'")
        root = Path(str(entry["root"])).expanduser()
        label = str(entry.get("label", "")).strip() or root.name
        if label in seen_labels:
            raise SourceConfigError(
                f"sources.yaml: duplicate label {label!r} — labels prefix citations "
                "and must identify exactly one source"
            )
        seen_ids.add(source_id)
        seen_labels.add(label)
        sources.append(
            Source(
                root=root,
                label=label,
                id=source_id,
                include=entry.get("include", ["**/*"]),
                exclude=entry.get("exclude", []),
            )
        )
    return sources


def _iter_files(source: Source) -> list[tuple[str, Path]]:
    """Yield (display_path, abs_path) for files matching the source's patterns."""
    seen: dict[str, Path] = {}
    for pattern in source.include:
        for path in source.root.rglob(pattern):
            if not path.is_file():
                continue
            rel = path.relative_to(source.root).as_posix()
            if any(part in _DEFAULT_EXCLUDE_DIRS for part in path.relative_to(source.root).parts):
                continue
            posix = path.as_posix()
            if any(fnmatch.fnmatch(posix, e) or fnmatch.fnmatch(rel, e) for e in source.exclude):
                continue
            display = f"{source.label}/{rel}"
            seen[display] = path
    return sorted(seen.items())


@dataclass
class FileRead:
    """Why a file is or isn't indexable. The distinction matters: a file we can
    never index should lose its stale vectors, while a file we merely failed to
    read this run must keep them."""

    text: str | None
    status: str
    reason: str = ""


def _read_text(path: Path) -> FileRead:
    try:
        size = path.stat().st_size
    except OSError as exc:
        return FileRead(None, STATUS_ERROR, f"stat failed: {type(exc).__name__}")
    if size > _MAX_FILE_BYTES:
        return FileRead(None, STATUS_SKIPPED, f"file exceeds {_MAX_FILE_BYTES} bytes")
    try:
        return FileRead(path.read_text(encoding="utf-8"), STATUS_INDEXED)
    except UnicodeDecodeError:
        # Not text. It will never be indexable in this form.
        return FileRead(None, STATUS_SKIPPED, "not valid UTF-8 text")
    except OSError as exc:
        # Locked, permission-denied, disappeared mid-walk: may well work next run.
        return FileRead(None, STATUS_ERROR, f"read failed: {type(exc).__name__}")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _init_manifest(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS files "
        "(path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, chunk_ids TEXT NOT NULL, "
        "indexed_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'indexed', "
        "reason TEXT NOT NULL DEFAULT '')"
    )
    # Pre-Phase-3 manifests predate the status columns.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
    if "status" not in existing:
        conn.execute("ALTER TABLE files ADD COLUMN status TEXT NOT NULL DEFAULT 'indexed'")
    if "reason" not in existing:
        conn.execute("ALTER TABLE files ADD COLUMN reason TEXT NOT NULL DEFAULT ''")
    conn.commit()


async def _embed_in_batches(texts: list[str], embed_fn: EmbedTextsFn) -> list[list[float]]:
    """Embed in bounded batches.

    One request per file would send an entire large file's chunks at once, which
    is what makes the embedder OOM or time out on exactly the files that most
    need indexing.
    """
    size = max(1, config.EMBED_BATCH_SIZE)
    vectors: list[list[float]] = []
    for start in range(0, len(texts), size):
        batch = texts[start : start + size]
        embedded = await embed_fn(batch)
        if len(embedded) != len(batch):
            raise ValueError(
                f"embedder returned {len(embedded)} vectors for {len(batch)} inputs"
            )
        vectors.extend(embedded)
    return vectors


def _record(
    conn: sqlite3.Connection,
    display: str,
    sha: str,
    chunk_ids: list[str],
    status: str,
    reason: str,
) -> None:
    conn.execute(
        "REPLACE INTO files (path, sha256, chunk_ids, indexed_at, status, reason) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (display, sha, json.dumps(chunk_ids), datetime.now(UTC).isoformat(), status, reason),
    )
    conn.commit()


async def run_ingest(
    sources: list[Source],
    *,
    client,
    embed_fn: EmbedTextsFn,
    state_db: str,
    collection: str,
    dim: int,
) -> dict[str, int]:
    ensure_collection(client, collection, dim)
    Path(state_db).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(state_db)
    try:
        _init_manifest(conn)
        manifest = {
            row[0]: (row[1], json.loads(row[2]))
            for row in conn.execute("SELECT path, sha256, chunk_ids FROM files")
        }

        current: dict[str, Path] = {}
        for source in sources:
            current.update(dict(_iter_files(source)))

        stats = {
            "files_total": 0,
            "changed": 0,
            "unchanged": 0,
            "deleted": 0,
            "chunks": 0,
            "skipped": 0,
            "errors": 0,
        }

        for display, abs_path in current.items():
            read = _read_text(abs_path)
            if read.text is None:
                old_ids = manifest.get(display, (None, []))[1]
                if read.status == STATUS_SKIPPED:
                    # It is no longer indexable, so its old vectors are wrong
                    # rather than merely stale — drop them.
                    if old_ids:
                        delete_by_chunk_ids(client, collection, old_ids)
                    _record(conn, display, "", [], STATUS_SKIPPED, read.reason)
                    stats["skipped"] += 1
                else:
                    # Transient: keep the last good vectors and say why.
                    if display in manifest:
                        _record(
                            conn,
                            display,
                            manifest[display][0],
                            old_ids,
                            STATUS_ERROR,
                            read.reason,
                        )
                    stats["errors"] += 1
                continue

            stats["files_total"] += 1
            text = read.text
            sha = _sha256(text)
            if display in manifest and manifest[display][0] == sha:
                stats["unchanged"] += 1
                continue

            chunks = chunk_file(display, text)
            new_ids = [c.chunk_id for c in chunks]
            old_ids = manifest.get(display, (None, []))[1]

            if chunks:
                try:
                    vectors = await _embed_in_batches([c.text for c in chunks], embed_fn)
                except Exception as exc:  # noqa: BLE001 - one bad file must not end the run
                    if display in manifest:
                        _record(
                            conn,
                            display,
                            manifest[display][0],
                            old_ids,
                            STATUS_ERROR,
                            f"embedding failed: {type(exc).__name__}",
                        )
                    stats["errors"] += 1
                    continue
                # Upsert first: until the new chunks are in, the old ones are the
                # only thing making this file findable.
                upsert_chunks(
                    client,
                    collection,
                    [
                        (c.chunk_id, v, {**c.payload(), "corpus_version": sha[:12]})
                        for c, v in zip(chunks, vectors, strict=True)
                    ],
                )

            stale = [cid for cid in old_ids if cid not in new_ids]
            if stale:
                delete_by_chunk_ids(client, collection, stale)
            _record(conn, display, sha, new_ids, STATUS_INDEXED, "")
            stats["changed"] += 1
            stats["chunks"] += len(chunks)

        for gone in set(manifest) - set(current):
            delete_by_chunk_ids(client, collection, manifest[gone][1])
            conn.execute("DELETE FROM files WHERE path = ?", (gone,))
            conn.commit()
            stats["deleted"] += 1

        return stats
    finally:
        conn.close()


def main() -> None:
    sources_path = Path(__file__).resolve().parent / "sources.yaml"
    try:
        sources = load_sources(sources_path)
    except SourceConfigError as exc:
        raise SystemExit(str(exc)) from exc
    if not sources:
        print("No sources configured in ingest/sources.yaml — nothing to do.")
        return
    client = get_client(config.QDRANT_URL)
    stats = asyncio.run(
        run_ingest(
            sources,
            client=client,
            embed_fn=embed_texts,
            state_db=config.STATE_DB,
            collection=config.QDRANT_COLLECTION,
            dim=config.EMBED_DIM,
        )
    )
    print(
        f"ingest complete: {stats['changed']} changed, {stats['unchanged']} unchanged, "
        f"{stats['deleted']} removed, {stats['skipped']} skipped, "
        f"{stats['errors']} errored, {stats['chunks']} chunks embedded"
    )


if __name__ == "__main__":
    main()
