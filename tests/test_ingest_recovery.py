"""Exercise recovery against real local Qdrant and SQLite, with no model/network access."""

import asyncio
import json
import sqlite3
from contextlib import closing

import pytest
from qdrant_client import QdrantClient

from ingest import ingest
from ingest.ingest import Source, SourceConfigError, run_ingest
from mcp_gateway import config
from retrieval.qdrant_store import delete_by_chunk_ids, ensure_collection, upsert_chunks


@pytest.fixture
def index(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "notes.md").write_text("# Notes\n\nKnowledge to preserve.", encoding="utf-8")
    source = Source(root=root, label="notes", id="stable", include=["**/*.md"])
    client = QdrantClient(":memory:")
    calls = []

    async def embed(texts):
        calls.append(len(texts))
        return [[1.0] * 8 for _ in texts]

    def run(sources=None, **kwargs):
        return asyncio.run(run_ingest(
            [source] if sources is None else sources, client=client,
            embed_fn=kwargs.pop("embed_fn", embed), state_db=str(tmp_path / "manifest.db"),
            collection=kwargs.pop("collection", "test"), dim=kwargs.pop("dim", 8), **kwargs,
        ))

    def rows(table="files"):
        with closing(sqlite3.connect(tmp_path / "manifest.db")) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]

    yield source, client, calls, run, rows
    client.close()


def test_missing_source_preserves_points_and_recovers(index):
    source, client, calls, run, rows = index
    run()
    source.root.rename(source.root.with_name("offline"))
    stats = run()
    assert stats["errors"] == stats["sources_unavailable"] == 1
    assert stats["deleted"] == 0
    assert client.count("test").count == 1
    assert rows()[0]["status"] == "error"
    source.root.with_name("offline").rename(source.root)
    assert run()["unchanged"] == 1
    assert len(calls) == 1
    assert rows()[0]["status"] == "indexed"


def test_partial_enumeration_cannot_delete_a_subtree(index, monkeypatch):
    source, client, _, run, rows = index
    run()

    def interrupted_walk(root, *, onerror, followlinks):
        yield str(root), [], []
        onerror(PermissionError("subtree unreadable"))

    monkeypatch.setattr(ingest.os, "walk", interrupted_walk)
    assert run()["sources_unavailable"] == 1
    assert client.count("test").count == 1
    assert "PermissionError" in rows("sources")[0]["reason"]


def test_unconfigured_source_requires_explicit_removal(index):
    _, client, _, run, rows = index
    run()
    assert run(sources=[])["sources_retained"] == 1
    assert client.count("test").count == 1
    assert run(sources=[], remove_source_ids=("stable",))["deleted"] == 1
    assert client.count("test").count == 0
    assert not rows()


def test_removal_rejects_configured_or_unknown_source(index):
    _, client, _, run, _ = index
    run()
    with pytest.raises(SourceConfigError, match="sources.yaml"):
        run(remove_source_ids=("stable",))
    with pytest.raises(SourceConfigError, match="unknown source"):
        run(sources=[], remove_source_ids=("typo",))
    assert client.count("test").count == 1


@pytest.mark.parametrize("loss", ["collection", "points", "one_point"])
def test_rebuilds_missing_vectors_with_unchanged_manifest(index, loss):
    source, client, calls, run, rows = index
    (source.root / "more.md").write_text("# More\n\nAnother fact.", encoding="utf-8")
    run()
    if loss == "collection":
        client.delete_collection("test")
    else:
        entries = rows()[:1] if loss == "one_point" else rows()
        for row in entries:
            delete_by_chunk_ids(client, "test", json.loads(row["chunk_ids"]))
    stats = run()
    assert stats["changed"] == (1 if loss == "one_point" else 2)
    assert client.count("test").count == 2
    assert len(calls) > 2


@pytest.mark.parametrize("name,value", [
    ("EMBED_MODEL", "replacement-model"),
    ("EMBED_MODEL_REVISION", "new-weights"),
    ("CHUNK_MAX_CHARS", 500),
    ("CHUNK_OVERLAP_CHARS", 25),
])
def test_pipeline_change_reembeds_unchanged_text(index, monkeypatch, name, value):
    _, _, calls, run, rows = index
    run()
    before = rows()[0]["fingerprint"]
    monkeypatch.setattr(config, name, value)
    assert run()["changed"] == 1
    assert rows()[0]["fingerprint"] != before
    assert len(calls) == 2
    assert run()["unchanged"] == 1


def test_pipeline_code_version_and_forced_rebuild(index, monkeypatch):
    _, _, _, run, _ = index
    run()
    monkeypatch.setattr(ingest, "PIPELINE_VERSION", 999)
    assert run()["changed"] == 1
    assert run(rebuild=True)["changed"] == 1


def test_dimension_mismatch_preserves_existing_collection(index):
    _, client, _, run, rows = index
    run()
    before = rows()
    with pytest.raises(ValueError, match="new QDRANT_COLLECTION"):
        run(dim=16)
    assert client.count("test").count == 1
    assert rows() == before


def test_source_move_and_relabel_preserve_identity(index):
    source, client, calls, run, rows = index
    run()
    initial_ids = [p.id for p in client.scroll("test")[0]]
    moved = source.root.with_name("moved")
    source.root.rename(moved)
    source.root = moved
    assert run()["unchanged"] == 1
    assert len(calls) == 1
    source.label = "renamed"
    assert run()["changed"] == 1
    points = client.scroll("test")[0]
    assert [p.id for p in points] == initial_ids
    assert points[0].payload["citation"].startswith("renamed/")
    assert len(rows()) == 1
    assert rows()[0]["source_id"] == "stable"


async def failed_embedding(_texts):
    raise RuntimeError("embedder down")


def test_first_failure_is_recorded_and_can_recover(index):
    _, client, _, run, rows = index
    assert run(embed_fn=failed_embedding)["errors"] == 1
    assert rows()[0]["status"] == "error"
    assert rows()[0]["chunk_ids"] == "[]"
    assert run()["changed"] == 1
    assert client.count("test").count == 1


def test_failed_model_upgrade_retains_old_fingerprint_and_retries(index, monkeypatch):
    _, client, _, run, rows = index
    run()
    old_fingerprint = rows()[0]["fingerprint"]
    monkeypatch.setattr(config, "EMBED_MODEL_REVISION", "replacement")
    assert run(embed_fn=failed_embedding)["errors"] == 1
    assert rows()[0]["fingerprint"] == old_fingerprint
    assert client.count("test").count == 1
    assert run()["changed"] == 1
    assert rows()[0]["fingerprint"] != old_fingerprint


def test_post_walk_read_failure_defers_deletions(index, monkeypatch):
    source, client, _, run, _ = index
    removed = source.root / "gone.md"
    removed.write_text("# Gone", encoding="utf-8")
    run()
    removed.unlink()
    with monkeypatch.context() as patch:
        patch.setattr(ingest, "_read_text", lambda path: ingest.FileRead(None, "error", "locked"))
        assert run()["deleted"] == 0
        assert client.count("test").count == 2
    assert run()["deleted"] == 1


def test_legacy_manifest_migration_removes_old_point_ids_only_after_success(index, tmp_path):
    _, client, _, run, rows = index
    ensure_collection(client, "test", 8)
    upsert_chunks(client, "test", [("legacy-id", [1.0] * 8, {"text": "last good"})])
    with closing(sqlite3.connect(tmp_path / "manifest.db")) as conn:
        conn.execute(
            "CREATE TABLE files (path TEXT PRIMARY KEY, sha256 TEXT, "
            "chunk_ids TEXT, indexed_at TEXT)"
        )
        conn.execute("INSERT INTO files VALUES (?, ?, ?, ?)",
                     ("notes/notes.md", "legacy-sha", '["legacy-id"]', "old"))
        conn.commit()
    assert run(embed_fn=failed_embedding)["errors"] == 1
    assert client.count("test").count == 1
    assert rows()[0]["source_id"] == "stable"
    assert run()["changed"] == 1
    assert client.count("test").count == 1
    assert "legacy-id" not in rows()[0]["chunk_ids"]
    with closing(sqlite3.connect(tmp_path / "manifest.db.v1.bak")) as backup:
        assert backup.execute("SELECT chunk_ids FROM files").fetchone()[0] == '["legacy-id"]'


def test_cli_errors_exit_nonzero_and_close_client(index, monkeypatch):
    source, client, _, _, _ = index
    monkeypatch.setattr("sys.argv", ["ingest"])
    monkeypatch.setattr(ingest, "load_sources", lambda path: [source])
    monkeypatch.setattr(ingest, "get_client", lambda url: client)
    closed = []
    monkeypatch.setattr(client, "close", lambda: closed.append(True))

    async def failed_run(*args, **kwargs):
        return dict.fromkeys(("changed", "unchanged", "deleted", "skipped", "errors", "chunks",
                              "sources_unavailable", "sources_retained"), 1)

    monkeypatch.setattr(ingest, "run_ingest", failed_run)
    with pytest.raises(SystemExit) as exc:
        ingest.main()
    assert exc.value.code == 1
    assert closed == [True]


def test_failed_relabel_repairs_citations_on_retry(index):
    source, client, _, run, _ = index
    run()
    source.label = "new-label"
    assert run(embed_fn=failed_embedding)["errors"] == 1
    assert run()["changed"] == 1
    assert client.scroll("test")[0][0].payload["citation"].startswith("new-label/")


def test_restored_stale_point_is_reembedded(index):
    _, client, _, run, _ = index
    run()
    point = client.scroll("test")[0][0]
    client.set_payload("test", payload={"content_sha256": "outdated-backup"}, points=[point.id])
    assert run()["changed"] == 1
    assert client.scroll("test")[0][0].payload["content_sha256"] != "outdated-backup"


def test_source_failure_does_not_prevent_healthy_source_progress(index, tmp_path):
    source, client, _, run, _ = index
    other_root = tmp_path / "healthy"
    other_root.mkdir()
    (other_root / "other.md").write_text("# Healthy", encoding="utf-8")
    other = Source(root=other_root, label="other", id="other", include=["**/*.md"])
    run(sources=[source, other])
    source.root.rename(source.root.with_name("offline"))
    (other_root / "other.md").write_text("# Healthy\nUpdated.", encoding="utf-8")
    stats = run(sources=[source, other])
    assert stats["changed"] == stats["sources_unavailable"] == 1
    assert client.count("test").count == 2


def test_relative_source_paths_do_not_depend_on_current_directory(tmp_path, monkeypatch):
    folder = tmp_path / "repo" / "ingest"
    folder.mkdir(parents=True)
    source_file = folder / "sources.yaml"
    source_file.write_text("sources:\n  - id: notes\n    label: notes\n    root: .\n")
    monkeypatch.chdir(tmp_path)
    assert ingest.load_sources(source_file)[0].root.resolve() == folder.parent


def test_failed_manifest_upgrade_rolls_back_schema(tmp_path, monkeypatch):
    from ingest.manifest import Manifest

    db_path = tmp_path / "state.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE files (path TEXT, sha256 TEXT, chunk_ids TEXT)")
        conn.execute("INSERT INTO files VALUES ('notes/a.md', 'old', '[]')")
        conn.commit()

        def fail(*args, **kwargs):
            raise RuntimeError("simulated interrupted migration")

        monkeypatch.setattr(Manifest, "save", fail)
        with pytest.raises(RuntimeError):
            Manifest(conn)
        assert conn.execute("SELECT path FROM files").fetchone()[0] == "notes/a.md"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='legacy_files'"
        ).fetchone()
    assert (tmp_path / "state.db.v1.bak").exists()


def test_future_manifest_schema_is_rejected_without_changes(tmp_path):
    from ingest.manifest import Manifest

    with closing(sqlite3.connect(tmp_path / "future.db")) as conn:
        conn.execute("PRAGMA user_version=999")
        with pytest.raises(ValueError, match="newer"):
            Manifest(conn)
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='files'").fetchone()
