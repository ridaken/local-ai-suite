"""Incremental ingest tests against an in-memory Qdrant + a fake embedder.

Verifies the core promise: only changed files are re-embedded, and deletes are
propagated to the vector store.
"""

import asyncio
from pathlib import Path

from qdrant_client import QdrantClient

from ingest.ingest import Source, _iter_files, run_ingest

DIM = 8


async def fake_embed(texts):
    # Deterministic, dimension-correct vectors; content doesn't matter for these tests.
    return [[float(len(t) % 7) + 1.0] * DIM for t in texts]


def _ingest(tmp_path: Path, client: QdrantClient):
    src = Source(root=tmp_path, label="proj", include=["**/*.py", "**/*.md"])
    return asyncio.run(
        run_ingest(
            [src],
            client=client,
            embed_fn=fake_embed,
            state_db=str(tmp_path / "state.db"),
            collection="test",
            dim=DIM,
        )
    )


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
