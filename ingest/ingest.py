"""Incremental ingest driver.

Walks the sources declared in sources.yaml, hashes each file, and re-embeds only
what changed since last run (tracked in a sqlite manifest). Changed files have
their stale chunks deleted from Qdrant before the new ones are upserted; files
removed from disk have their chunks deleted too. This is what makes "re-index on
save" cheap — embedding, the only costly step, touches changed files only.

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

_DEFAULT_EXCLUDES = ["*/.git/*", "*/.venv/*", "*/node_modules/*", "*/__pycache__/*"]
_MAX_FILE_BYTES = 1_000_000


@dataclass
class Source:
    root: Path
    label: str
    include: list[str] = field(default_factory=lambda: ["**/*"])
    exclude: list[str] = field(default_factory=list)


def load_sources(sources_path: Path) -> list[Source]:
    raw = yaml.safe_load(sources_path.read_text(encoding="utf-8")) or {}
    sources = []
    for entry in raw.get("sources", []):
        root = Path(entry["root"]).expanduser()
        sources.append(
            Source(
                root=root,
                label=entry.get("label", root.name),
                include=entry.get("include", ["**/*"]),
                exclude=entry.get("exclude", []),
            )
        )
    return sources


def _iter_files(source: Source) -> list[tuple[str, Path]]:
    """Yield (display_path, abs_path) for files matching the source's patterns."""
    excludes = _DEFAULT_EXCLUDES + source.exclude
    seen: dict[str, Path] = {}
    for pattern in source.include:
        for path in source.root.rglob(pattern):
            if not path.is_file():
                continue
            rel = path.relative_to(source.root).as_posix()
            posix = path.as_posix()
            if any(fnmatch.fnmatch(posix, e) or fnmatch.fnmatch(rel, e) for e in excludes):
                continue
            display = f"{source.label}/{rel}"
            seen[display] = path
    return sorted(seen.items())


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _init_manifest(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS files "
        "(path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, chunk_ids TEXT NOT NULL, "
        "indexed_at TEXT NOT NULL)"
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

        stats = {"files_total": 0, "changed": 0, "unchanged": 0, "deleted": 0, "chunks": 0}

        for display, abs_path in current.items():
            text = _read_text(abs_path)
            if text is None:
                continue
            stats["files_total"] += 1
            sha = _sha256(text)
            if display in manifest and manifest[display][0] == sha:
                stats["unchanged"] += 1
                continue

            chunks = chunk_file(display, text)
            new_ids = [c.chunk_id for c in chunks]
            old_ids = manifest.get(display, (None, []))[1]
            stale = [cid for cid in old_ids if cid not in new_ids]
            if stale:
                delete_by_chunk_ids(client, collection, stale)

            if chunks:
                vectors = await embed_fn([c.text for c in chunks])
                upsert_chunks(
                    client,
                    collection,
                    [(c.chunk_id, v, c.payload()) for c, v in zip(chunks, vectors, strict=True)],
                )
            conn.execute(
                "REPLACE INTO files (path, sha256, chunk_ids, indexed_at) VALUES (?, ?, ?, ?)",
                (display, sha, json.dumps(new_ids), datetime.now(UTC).isoformat()),
            )
            stats["changed"] += 1
            stats["chunks"] += len(chunks)

        for gone in set(manifest) - set(current):
            delete_by_chunk_ids(client, collection, manifest[gone][1])
            conn.execute("DELETE FROM files WHERE path = ?", (gone,))
            stats["deleted"] += 1

        conn.commit()
        return stats
    finally:
        conn.close()


def main() -> None:
    sources_path = Path(__file__).resolve().parent / "sources.yaml"
    sources = load_sources(sources_path)
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
        f"{stats['deleted']} removed, {stats['chunks']} chunks embedded"
    )


if __name__ == "__main__":
    main()
