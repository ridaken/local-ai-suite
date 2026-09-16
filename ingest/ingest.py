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

import argparse
import asyncio
import fnmatch
import hashlib
import json
import os
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from mcp_gateway import config
from retrieval.embed import embed_texts
from retrieval.qdrant_store import (
    chunks_current,
    delete_by_chunk_ids,
    ensure_collection,
    get_client,
    upsert_chunks,
)

from .chunking import chunk_file
from .manifest import Entry, Manifest, file_key

EmbedTextsFn = Callable[[list[str]], Awaitable[list[list[float]]]]

_DEFAULT_EXCLUDE_DIRS = {".git", ".venv", "node_modules", "__pycache__"}
_MAX_FILE_BYTES = 1_000_000
# Bump when chunking/embedding preprocessing changes, even without a setting change.
PIPELINE_VERSION = 1

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
    if not isinstance(raw, dict):
        raise SourceConfigError("sources.yaml must be a mapping")
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
        # Preserve documented repo-relative roots, independent of the caller's cwd.
        if not root.is_absolute():
            root = sources_path.resolve().parent.parent / root
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
    _validate_sources(sources)
    return sources


def _validate_sources(sources: list[Source]) -> None:
    if len({s.id for s in sources}) != len(sources):
        raise SourceConfigError("duplicate source id")
    if len({s.label for s in sources}) != len(sources):
        raise SourceConfigError("duplicate source label")
    for source in sources:
        if not source.id or not source.label or "/" in source.label or "\\" in source.label:
            raise SourceConfigError("sources need a stable id and a label without path separators")
        for patterns in (source.include, source.exclude):
            if not isinstance(patterns, list) or not all(
                isinstance(p, str) and p and not Path(p).is_absolute()
                and ".." not in Path(p).parts for p in patterns
            ):
                raise SourceConfigError("include/exclude must be lists of relative glob patterns")


def _iter_files(source: Source) -> list[tuple[str, Path]]:
    """Complete a strict walk before returning; never interpret a failed scan as deletion."""
    if not source.root.is_dir():
        raise OSError("source root is unavailable")

    def failed(exc: OSError) -> None:
        raise exc

    seen: dict[str, Path] = {}
    for directory, dirs, files in os.walk(source.root, onerror=failed, followlinks=False):
        dirs[:] = [d for d in dirs if d not in _DEFAULT_EXCLUDE_DIRS]
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(source.root)
            if not any(
                relative.match(pattern) or relative.match(pattern.removeprefix("**/"))
                for pattern in source.include
            ):
                continue
            rel = relative.as_posix()
            posix = path.as_posix()
            if any(fnmatch.fnmatch(posix, e) or fnmatch.fnmatch(rel, e) for e in source.exclude):
                continue
            display = f"{source.label}/{rel}"
            seen[display] = path
    if not source.root.is_dir():
        raise OSError("source root disappeared during enumeration")
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


def pipeline_fingerprint(source_id: str, collection: str, dim: int) -> str:
    return _sha256(json.dumps({
        "version": PIPELINE_VERSION, "source_id": source_id, "collection": collection,
        "model": config.EMBED_MODEL, "model_revision": config.EMBED_MODEL_REVISION,
        "dimension": dim, "chunk_max": config.CHUNK_MAX_CHARS,
        "chunk_overlap": config.CHUNK_OVERLAP_CHARS,
    }, sort_keys=True))


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


async def _index_file(
    entry: Entry, abs_path: Path, *, manifest: Manifest, client, collection: str,
    fingerprint: str, embed_fn: EmbedTextsFn, rebuild: bool,
) -> tuple[str, int]:
    read = _read_text(abs_path)
    if read.text is None:
        if read.status == STATUS_SKIPPED:
            delete_by_chunk_ids(client, collection, entry.chunk_ids)
            entry.sha256, entry.chunk_ids, entry.fingerprint = "", [], ""
        entry.status, entry.reason = read.status, read.reason
        manifest.save(entry)
        return ("skipped" if read.status == STATUS_SKIPPED else "errors"), 0

    sha = _sha256(read.text)
    if (
        not rebuild and entry.sha256 == sha and entry.fingerprint == fingerprint
        and chunks_current(client, collection, entry.chunk_ids, fingerprint, sha, entry.path)
    ):
        # Refresh display metadata without changing identity or paying for embeddings.
        if entry.status != STATUS_INDEXED:
            entry.status, entry.reason = STATUS_INDEXED, ""
            manifest.save(entry)
        return "unchanged", 0

    chunks = chunk_file(entry.path, read.text)
    for chunk in chunks:
        chunk.chunk_id = json.dumps(
            [entry.source_id, entry.relative_path, chunk.symbol, chunk.start_line, chunk.end_line],
            separators=(",", ":"),
        )
    new_ids = [chunk.chunk_id for chunk in chunks]
    vectors = await _embed_in_batches([chunk.text for chunk in chunks], embed_fn)
    upsert_chunks(client, collection, [
        (chunk.chunk_id, vector, {
            **chunk.payload(), "corpus_version": sha[:12], "content_sha256": sha,
            "pipeline_fingerprint": fingerprint, "source_id": entry.source_id,
            "relative_path": entry.relative_path,
        })
        for chunk, vector in zip(chunks, vectors, strict=True)
    ])
    stale = [cid for cid in entry.chunk_ids if cid not in new_ids]
    delete_by_chunk_ids(client, collection, stale)
    entry.sha256, entry.chunk_ids, entry.fingerprint = sha, new_ids, fingerprint
    entry.status, entry.reason = STATUS_INDEXED, ""
    manifest.save(entry)
    return "changed", len(chunks)


async def run_ingest(
    sources: list[Source],
    *,
    client,
    embed_fn: EmbedTextsFn,
    state_db: str,
    collection: str,
    dim: int,
    rebuild: bool = False,
    remove_source_ids: tuple[str, ...] = (),
) -> dict[str, int]:
    _validate_sources(sources)
    configured_ids = {s.id for s in sources}
    if configured_ids.intersection(remove_source_ids):
        raise SourceConfigError("remove a source from sources.yaml before using --remove-source")
    ensure_collection(client, collection, dim)
    Path(state_db).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(state_db)
    try:
        manifest = Manifest(conn)
        manifest.adopt_sources(sources)
        previous = manifest.entries()
        stats = dict.fromkeys(
            ("files_total", "changed", "unchanged", "deleted", "chunks", "skipped", "errors",
             "sources_unavailable", "sources_removed", "sources_retained"), 0
        )
        # Only an explicit source removal may delete entries of an unconfigured source.
        known = {row[0] for row in conn.execute("SELECT id FROM sources")}
        unknown = set(remove_source_ids) - known
        if unknown:
            raise SourceConfigError(f"unknown source IDs requested for removal: {sorted(unknown)}")
        for source_id in remove_source_ids:
            for entry in previous.values():
                if entry.source_id == source_id:
                    delete_by_chunk_ids(client, collection, entry.chunk_ids)
                    manifest.delete(entry)
                    stats["deleted"] += 1
            with conn:
                conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
            stats["sources_removed"] += 1
        for source_id in known - configured_ids - set(remove_source_ids):
            manifest.source_status(
                source_id, "retained", "not configured; explicit removal required"
            )
            stats["sources_retained"] += 1

        for source in sources:
            fingerprint = pipeline_fingerprint(source.id, collection, dim)
            old = {key: entry for key, entry in previous.items() if entry.source_id == source.id}
            try:
                files = _iter_files(source)
            except OSError as exc:
                reason = f"source scan failed: {type(exc).__name__}"
                manifest.source_status(source.id, STATUS_ERROR, reason)
                for entry in old.values():
                    entry.status, entry.reason = STATUS_ERROR, reason
                    manifest.save(entry)
                stats["errors"] += 1
                stats["sources_unavailable"] += 1
                continue

            current = set()
            errors_before = stats["errors"]
            for display, abs_path in files:
                relative = abs_path.relative_to(source.root).as_posix()
                key = file_key(source.id, relative)
                current.add(key)
                entry = old.get(key) or Entry(source.id, relative, display)
                # A label rename updates citations by re-upserting, but preserves point IDs.
                renamed = entry.path != display
                entry.path = display
                stats["files_total"] += 1
                try:
                    outcome, count = await _index_file(
                        entry, abs_path, manifest=manifest, client=client, collection=collection,
                        fingerprint=fingerprint, embed_fn=embed_fn, rebuild=rebuild or renamed,
                    )
                except Exception as exc:  # noqa: BLE001 - retain the last good manifest for retry
                    entry.status = STATUS_ERROR
                    entry.reason = f"indexing/embedding failed: {type(exc).__name__}"
                    manifest.save(entry)
                    outcome, count = "errors", 0
                stats[outcome] += 1
                stats["chunks"] += count

            # A file can vanish or become unreadable after enumeration. Preserve all
            # deletions for that source until a complete, healthy run confirms them.
            if stats["errors"] == errors_before:
                for key in old.keys() - current:
                    entry = old[key]
                    delete_by_chunk_ids(client, collection, entry.chunk_ids)
                    manifest.delete(entry)
                    stats["deleted"] += 1
                manifest.source_status(source.id, STATUS_INDEXED)
            else:
                manifest.source_status(source.id, STATUS_ERROR, "one or more files failed indexing")
        return stats
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Incrementally index curated sources.")
    parser.add_argument(
        "--sources", type=Path, default=Path(__file__).resolve().parent / "sources.yaml"
    )
    parser.add_argument("--rebuild", action="store_true", help="re-embed all available files")
    parser.add_argument(
        "--remove-source", action="append", default=[], metavar="ID",
        help="explicitly delete an unconfigured source's index and manifest entries",
    )
    args = parser.parse_args()
    try:
        sources = load_sources(args.sources)
    except SourceConfigError as exc:
        raise SystemExit(str(exc)) from exc
    client = get_client(config.QDRANT_URL)
    try:
        stats = asyncio.run(run_ingest(
            sources, client=client, embed_fn=embed_texts, state_db=config.STATE_DB,
            collection=config.QDRANT_COLLECTION, dim=config.EMBED_DIM, rebuild=args.rebuild,
            remove_source_ids=tuple(args.remove_source),
        ))
    finally:
        client.close()
    print(
        f"ingest complete: {stats['changed']} changed, {stats['unchanged']} unchanged, "
        f"{stats['deleted']} removed, {stats['skipped']} skipped, "
        f"{stats['errors']} errored, {stats['chunks']} chunks embedded; "
        f"{stats['sources_unavailable']} sources unavailable, "
        f"{stats['sources_retained']} unconfigured sources retained"
    )
    if stats["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
