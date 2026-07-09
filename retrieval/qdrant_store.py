"""Thin Qdrant wrapper: create the collection, upsert chunk vectors, search, and
delete points for changed/removed files (incremental ingest).

Point ids are deterministic UUIDs derived from the chunk id, so re-ingesting a
changed file overwrites its old points and deletes are addressable by chunk id.
"""

from __future__ import annotations

import uuid
from typing import Any

from qdrant_client import QdrantClient, models

_NAMESPACE = uuid.UUID("6f8d3b2a-1c4e-4f5a-9b7c-0e1d2a3b4c5d")


def get_client(url: str) -> QdrantClient:
    """Server client from a URL, or a local/in-memory client (':memory:' or a path)."""
    if url in ("", ":memory:"):
        return QdrantClient(":memory:")
    if "://" not in url:
        return QdrantClient(path=url)
    return QdrantClient(url=url)


def point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, chunk_id))


def ensure_collection(client: QdrantClient, name: str, dim: int) -> None:
    if not client.collection_exists(name):
        client.create_collection(
            collection_name=name,
            vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE),
        )


def upsert_chunks(
    client: QdrantClient,
    name: str,
    items: list[tuple[str, list[float], dict[str, Any]]],
) -> None:
    """items = [(chunk_id, vector, payload), ...]. Payload should include chunk_id."""
    if not items:
        return
    points = [
        models.PointStruct(id=point_id(cid), vector=vec, payload={**payload, "chunk_id": cid})
        for cid, vec, payload in items
    ]
    client.upsert(collection_name=name, points=points)


def delete_by_chunk_ids(client: QdrantClient, name: str, chunk_ids: list[str]) -> None:
    if not chunk_ids:
        return
    client.delete(
        collection_name=name,
        points_selector=models.PointIdsList(points=[point_id(c) for c in chunk_ids]),
    )


def search(
    client: QdrantClient, name: str, vector: list[float], limit: int
) -> list[tuple[dict[str, Any], float]]:
    if not client.collection_exists(name):
        return []
    resp = client.query_points(
        collection_name=name, query=vector, limit=limit, with_payload=True
    )
    return [(p.payload or {}, p.score) for p in resp.points]
