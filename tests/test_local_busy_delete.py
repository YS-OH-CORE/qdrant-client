"""A real SQLite COMMIT failure must not leak a delete into the next write."""

import inspect
import sqlite3

import pytest

from qdrant_client import AsyncQdrantClient, QdrantClient, models


async def _call(client, method, *args, **kwargs):
    result = getattr(client, method)(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


def _point(point_id):
    return models.PointStruct(
        id=point_id,
        vector={
            "dense": [float(point_id), 1.0],
            "sparse": models.SparseVector(indices=[1, point_id + 10], values=[1.0, 0.5]),
        },
        payload={"selected": point_id <= 3},
    )


async def _snapshot(client):
    points, _ = await _call(client, "scroll", "items", limit=100, with_vectors=True)
    dense = await _call(client, "query_points", "items", [1.0, 0.0], using="dense", limit=100)
    sparse = await _call(
        client,
        "query_points",
        "items",
        models.SparseVector(indices=[1], values=[1.0]),
        using="sparse",
        limit=100,
    )
    return {
        "ids": sorted(point.id for point in points),
        "points": {point.id: point.model_dump() for point in points},
        "count": (await _call(client, "count", "items", exact=True)).count,
        "retrieved": sorted(
            point.id for point in await _call(client, "retrieve", "items", [1, 2, 3, 9, 10])
        ),
        "dense": {point.id: point.score for point in dense.points},
        "sparse": {point.id: point.score for point in sparse.points},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("client_type", [QdrantClient, AsyncQdrantClient], ids=["sync", "async"])
@pytest.mark.parametrize("selector_kind", ["ids", "filter"])
@pytest.mark.parametrize("lock_on_delete", [1, 2], ids=["first-point", "committed-prefix"])
async def test_busy_delete_does_not_commit_with_an_unrelated_write(
    tmp_path,
    client_type,
    selector_kind,
    lock_on_delete,
):
    """Use a read-only SQL transaction, not a mocked delete/commit exception.

    After one point has committed, rollback of a later busy transaction must
    preserve that progress but leave the failed and unreached points untouched.
    Public operations are sequential; no concurrent Qdrant writer is used.
    """
    path = str(tmp_path / "store")
    client = client_type(path=path)
    reader = None
    try:
        await _call(
            client,
            "create_collection",
            "items",
            vectors_config={"dense": models.VectorParams(size=2, distance=models.Distance.DOT)},
            sparse_vectors_config={
                "sparse": models.SparseVectorParams(modifier=models.Modifier.IDF)
            },
        )
        await _call(client, "upsert", "items", [_point(i) for i in (1, 2, 3, 9)])
        collection = client._client._get_collection("items")
        storage = collection.storage.storage
        assert storage.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        storage.execute("PRAGMA busy_timeout=30")
        reader = sqlite3.connect(
            collection.storage.location.resolve().as_uri() + "?mode=ro", uri=True
        )
        trace = []
        deletes = 0
        lock_errors = []

        def hold_reader_at_delete(statement):
            nonlocal deletes
            if statement.startswith("DELETE FROM points"):
                deletes += 1
                if deletes == lock_on_delete:
                    try:
                        reader.execute("BEGIN")
                        reader.execute("SELECT id FROM points").fetchall()
                    except Exception as exc:
                        lock_errors.append(exc)
            trace.append(statement.split()[0])

        storage.set_trace_callback(hold_reader_at_delete)
        selector = (
            [1, 2, 3]
            if selector_kind == "ids"
            else models.Filter(
                must=[models.FieldCondition(key="selected", match=models.MatchValue(value=True))]
            )
        )
        with pytest.raises(sqlite3.OperationalError) as caught:
            await _call(client, "delete", "items", points_selector=selector)
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert not lock_errors
        assert deletes == lock_on_delete
        assert trace.count("COMMIT") == lock_on_delete
        pending_after_failure = storage.in_transaction
        storage.set_trace_callback(None)
        reader.rollback()
        reader.close()
        reader = None

        # Continuing with a different point must not commit a failed deletion.
        await _call(client, "upsert", "items", [_point(10)])
        live = await _snapshot(client)
        await _call(client, "close")
        client = client_type(path=path)
        reopened = await _snapshot(client)
        expected_ids = list(range(lock_on_delete, 4)) + [9, 10]
        assert live["ids"] == reopened["ids"] == expected_ids, {
            "live": live["ids"],
            "reopened": reopened["ids"],
            "pending_after_failure": pending_after_failure,
        }
        assert not pending_after_failure
        assert live["points"] == reopened["points"]
        assert live["count"] == reopened["count"] == len(expected_ids)
        assert live["retrieved"] == reopened["retrieved"] == expected_ids
        assert live["dense"] == pytest.approx(reopened["dense"])
        assert live["sparse"] == pytest.approx(reopened["sparse"])

        await _call(client, "delete", "items", points_selector=selector)
        retried = await _snapshot(client)
        assert retried["ids"] == [9, 10]
        await _call(client, "close")
        client = client_type(path=path)
        final = await _snapshot(client)
        assert final["points"] == retried["points"]
        assert final["dense"] == pytest.approx(retried["dense"])
        assert final["sparse"] == pytest.approx(retried["sparse"])
    finally:
        if reader is not None:
            reader.rollback()
            reader.close()
        await _call(client, "close")
