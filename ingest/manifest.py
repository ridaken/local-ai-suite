"""Versioned ingest state, keyed by source identity rather than display labels."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 2


def file_key(source_id: str, relative_path: str) -> str:
    return json.dumps([source_id, relative_path], separators=(",", ":"))


@dataclass
class Entry:
    source_id: str
    relative_path: str
    path: str
    sha256: str = ""
    chunk_ids: list[str] = field(default_factory=list)
    fingerprint: str = ""
    status: str = "error"
    reason: str = ""

    @property
    def key(self) -> str:
        return file_key(self.source_id, self.relative_path)


class Manifest:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise ValueError("ingest manifest was created by a newer application version")
        columns = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
        if columns and "file_key" not in columns:
            database_path = conn.execute("PRAGMA database_list").fetchone()[2]
            if database_path:
                backup = Path(database_path + ".v1.bak")
                # Never overwrite an earlier recovery point. A failed migration may
                # be retried; its schema changes are committed as one transaction.
                if not backup.exists():
                    with backup.open("xb"):
                        pass
                    try:
                        with closing(sqlite3.connect(backup)) as target:
                            conn.backup(target)
                    except BaseException:
                        backup.unlink(missing_ok=True)
                        raise
        conn.execute("BEGIN IMMEDIATE")
        with conn:
            if columns and "file_key" not in columns:
                conn.execute("ALTER TABLE files RENAME TO legacy_files")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS files ("
                "file_key TEXT PRIMARY KEY, source_id TEXT NOT NULL, "
                "relative_path TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL, "
                "chunk_ids TEXT NOT NULL, fingerprint TEXT NOT NULL, indexed_at TEXT NOT NULL, "
                "status TEXT NOT NULL, reason TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS sources ("
                "id TEXT PRIMARY KEY, label TEXT NOT NULL, "
                "status TEXT NOT NULL, reason TEXT NOT NULL)"
            )
            if columns and "file_key" not in columns:
                # Preserve unmapped legacy rows. Their old label is the initial source ID;
                # adopt_sources remaps it to a configured stable ID before any work.
                for path, sha, ids in conn.execute(
                    "SELECT path, sha256, chunk_ids FROM legacy_files"
                ).fetchall():
                    label, _, relative = path.partition("/")
                    self.save(Entry(label, relative, path, sha, json.loads(ids)), commit=False)
                    conn.execute(
                        "INSERT OR IGNORE INTO sources VALUES (?, ?, 'legacy', '')", (label, label)
                    )
                conn.execute("DROP TABLE legacy_files")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def adopt_sources(self, sources) -> None:  # noqa: ANN001
        """Map legacy labels once. Ambiguous mappings fail before vectors are changed."""
        with self.conn:
            for source in sources:
                row = self.conn.execute(
                    "SELECT id FROM sources WHERE label = ? AND status = 'legacy'", (source.label,)
                ).fetchone()
                if row and row[0] != source.id:
                    if self.conn.execute(
                        "SELECT 1 FROM sources WHERE id = ?", (source.id,)
                    ).fetchone():
                        raise ValueError("ambiguous legacy source mapping; restore old labels")
                    for entry in self.entries().values():
                        if entry.source_id != row[0]:
                            continue
                        old_key = entry.key
                        entry.source_id = source.id
                        if self.conn.execute(
                            "SELECT 1 FROM files WHERE file_key = ?", (entry.key,)
                        ).fetchone():
                            raise ValueError("ambiguous legacy source mapping; restore old labels")
                        self.conn.execute("DELETE FROM files WHERE file_key = ?", (old_key,))
                        self.save(entry, commit=False)
                    self.conn.execute("DELETE FROM sources WHERE id = ?", (row[0],))
                self.conn.execute(
                    "INSERT INTO sources VALUES (?, ?, 'pending', '') "
                    "ON CONFLICT(id) DO UPDATE SET label=excluded.label",
                    (source.id, source.label),
                )

    def entries(self) -> dict[str, Entry]:
        entries = {}
        for row in self.conn.execute(
            "SELECT source_id, relative_path, path, sha256, chunk_ids, fingerprint, status, reason "
            "FROM files"
        ):
            entry = Entry(*row[:4], json.loads(row[4]), *row[5:])
            entries[entry.key] = entry
        return entries

    def save(self, entry: Entry, *, commit: bool = True) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (entry.key, entry.source_id, entry.relative_path, entry.path, entry.sha256,
             json.dumps(entry.chunk_ids), entry.fingerprint, datetime.now(UTC).isoformat(),
             entry.status, entry.reason),
        )
        if commit:
            self.conn.commit()

    def source_status(self, source_id: str, status: str, reason: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE sources SET status=?, reason=? WHERE id=?", (status, reason, source_id)
            )

    def delete(self, entry: Entry) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM files WHERE file_key=?", (entry.key,))
