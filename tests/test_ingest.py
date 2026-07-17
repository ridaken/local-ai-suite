"""Incremental ingest tests against an in-memory Qdrant + a fake embedder.

Verifies the core promise: only changed files are re-embedded, and deletes are
propagated to the vector store.
"""

import asyncio
import sqlite3
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from ingest.ingest import (
    STATUS_ERROR,
    STATUS_SKIPPED,
    Source,
    SourceConfigError,
    _iter_files,
    load_sources,
    run_ingest,
)

DIM = 8


async def fake_embed(texts):
    # Deterministic, dimension-correct vectors; content doesn't matter for these tests.
    return [[float(len(t) % 7) + 1.0] * DIM for t in texts]


def _ingest(tmp_path: Path, client: QdrantClient, embed_fn=fake_embed):
    src = Source(root=tmp_path, label="proj", id="proj", include=["**/*.py", "**/*.md"])
    return asyncio.run(
        run_ingest(
            [src],
            client=client,
            embed_fn=embed_fn,
            state_db=str(tmp_path / "state.db"),
            collection="test",
            dim=DIM,
        )
    )


def _manifest(tmp_path: Path) -> dict[str, tuple[str, str]]:
    conn = sqlite3.connect(tmp_path / "state.db")
    try:
        return {row[0]: (row[1], row[2]) for row in conn.execute(
            "SELECT path, status, reason FROM files"
        )}
    finally:
        conn.close()


def test_incremental_reindex(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("# Notes\n\nsome text\n", encoding="utf-8")
    client = QdrantClient(":memory:")

    first = _ingest(tmp_path, client)
    assert first["changed"] == 2
    assert first["chunks"] >= 2
    points_after_first = client.count("test").count
    assert points_after_first == first["chunks"]

    # Re-run with no changes: nothing re-embedded.
    second = _ingest(tmp_path, client)
    assert second["changed"] == 0
    assert second["unchanged"] == 2
    assert second["chunks"] == 0
    assert client.count("test").count == points_after_first


def test_changed_file_is_reembedded(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    client = QdrantClient(":memory:")
    _ingest(tmp_path, client)

    (tmp_path / "a.py").write_text("def f():\n    return 999\n", encoding="utf-8")
    result = _ingest(tmp_path, client)
    assert result["changed"] == 1


def test_deleted_file_removes_its_chunks(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def g():\n    return 2\n", encoding="utf-8")
    client = QdrantClient(":memory:")
    _ingest(tmp_path, client)
    total = client.count("test").count

    (tmp_path / "b.py").unlink()
    result = _ingest(tmp_path, client)
    assert result["deleted"] == 1
    assert client.count("test").count < total


def test_default_excludes_skip_nested_internal_dirs(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "nested" / ".git" / "objects").mkdir(parents=True)
    (tmp_path / "nested" / ".git" / "objects" / "blob.py").write_text(
        "print('skip')\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "node_modules" / "dep").mkdir(parents=True)
    (tmp_path / "pkg" / "node_modules" / "dep" / "index.py").write_text(
        "print('skip')\n", encoding="utf-8"
    )

    files = _iter_files(Source(root=tmp_path, label="proj", include=["**/*.py"]))
    assert [display for display, _ in files] == ["proj/src/app.py"]


def _write_sources(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_sources_require_a_stable_id(tmp_path):
    path = _write_sources(tmp_path, "sources:\n  - root: .\n    label: proj\n")
    with pytest.raises(SourceConfigError, match="missing a stable 'id'"):
        load_sources(path)


def test_duplicate_source_ids_are_rejected(tmp_path):
    path = _write_sources(
        tmp_path,
        "sources:\n"
        "  - id: same\n    root: a\n    label: a\n"
        "  - id: same\n    root: b\n    label: b\n",
    )
    with pytest.raises(SourceConfigError, match="duplicate source id"):
        load_sources(path)


def test_duplicate_labels_are_rejected(tmp_path):
    # Labels prefix the manifest key and the citation: two sources sharing one
    # would overwrite each other's manifest rows.
    path = _write_sources(
        tmp_path,
        "sources:\n"
        "  - id: one\n    root: a\n    label: same\n"
        "  - id: two\n    root: b\n    label: same\n",
    )
    with pytest.raises(SourceConfigError, match="duplicate label"):
        load_sources(path)


def test_valid_sources_load(tmp_path):
    path = _write_sources(
        tmp_path,
        'sources:\n  - id: notes\n    root: ~/notes\n    label: notes\n    include: ["**/*.md"]\n',
    )
    sources = load_sources(path)
    assert [(s.id, s.label, s.include) for s in sources] == [("notes", "notes", ["**/*.md"])]


def test_binary_file_is_skipped_and_loses_stale_vectors(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("def f():\n    return 1\n", encoding="utf-8")
    client = QdrantClient(":memory:")
    _ingest(tmp_path, client)
    assert client.count("test").count > 0

    # The file is still tracked but is no longer text: its old vectors now
    # describe content that does not exist.
    target.write_bytes(b"\xff\xfe\x00\x01binary")
    result = _ingest(tmp_path, client)

    assert result["skipped"] == 1
    assert client.count("test").count == 0
    status, reason = _manifest(tmp_path)["proj/a.py"]
    assert status == STATUS_SKIPPED
    assert "UTF-8" in reason


def test_oversized_file_is_skipped_with_a_reason(tmp_path):
    (tmp_path / "big.py").write_text("x = 1\n" * 400_000, encoding="utf-8")
    client = QdrantClient(":memory:")
    result = _ingest(tmp_path, client)

    assert result["skipped"] == 1
    status, reason = _manifest(tmp_path)["proj/big.py"]
    assert status == STATUS_SKIPPED
    assert "exceeds" in reason


def test_embedding_failure_retains_the_last_good_index(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    client = QdrantClient(":memory:")
    _ingest(tmp_path, client)
    good = client.count("test").count

    async def boom(_texts):
        raise RuntimeError("embedder down")

    (tmp_path / "a.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    result = _ingest(tmp_path, client, embed_fn=boom)

    # Transient failure: the previous vectors are still searchable, and the
    # manifest says why the file is stale.
    assert result["errors"] == 1
    assert result["changed"] == 0
    assert client.count("test").count == good
    status, reason = _manifest(tmp_path)["proj/a.py"]
    assert status == STATUS_ERROR
    assert "embedding failed" in reason

    # And the next healthy run picks it back up.
    recovered = _ingest(tmp_path, client)
    assert recovered["changed"] == 1
    assert _manifest(tmp_path)["proj/a.py"][0] == "indexed"


def test_embedding_is_batched(tmp_path, monkeypatch):
    monkeypatch.setattr("mcp_gateway.config.EMBED_BATCH_SIZE", 2)
    monkeypatch.setattr("mcp_gateway.config.CHUNK_MAX_CHARS", 256)
    (tmp_path / "a.py").write_text(
        "\n".join(f"def f{i}():\n    return {i}\n" for i in range(9)), encoding="utf-8"
    )
    batches = []

    async def spy(texts):
        batches.append(len(texts))
        return await fake_embed(texts)

    _ingest(tmp_path, QdrantClient(":memory:"), embed_fn=spy)
    assert batches, "embedder should have been called"
    assert max(batches) <= 2


def test_new_chunks_are_upserted_before_stale_ones_are_deleted(tmp_path, monkeypatch):
    """A crash between the two must never leave the file unsearchable."""
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    client = QdrantClient(":memory:")
    _ingest(tmp_path, client)

    calls = []
    import ingest.ingest as ingest_mod

    real_upsert = ingest_mod.upsert_chunks
    real_delete = ingest_mod.delete_by_chunk_ids

    def spy_upsert(*a, **kw):
        calls.append("upsert")
        return real_upsert(*a, **kw)

    def spy_delete(*a, **kw):
        calls.append("delete")
        return real_delete(*a, **kw)

    monkeypatch.setattr(ingest_mod, "upsert_chunks", spy_upsert)
    monkeypatch.setattr(ingest_mod, "delete_by_chunk_ids", spy_delete)

    # Rename the symbol so the old chunk id goes stale and must be deleted.
    (tmp_path / "a.py").write_text("def renamed():\n    return 1\n", encoding="utf-8")
    _ingest(tmp_path, client)

    assert calls.index("upsert") < calls.index("delete")


def test_chunks_carry_the_source_version(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    client = QdrantClient(":memory:")
    _ingest(tmp_path, client)
    points = client.scroll("test", limit=10, with_payload=True)[0]
    assert points
    assert all(p.payload.get("corpus_version") for p in points)
