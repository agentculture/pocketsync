"""Opt-in integration test against a real ``mongodb/mongodb-atlas-local`` container.

Skipped unless ``POCKETSYNC_IT_MONGO=1``; the URI comes from
``POCKETSYNC_IT_MONGO_URI`` (loopback only: the default socket guard in
``tests/conftest.py`` still applies). All data is synthetic. Each test run uses
a throwaway database that is dropped afterwards.

Start a throwaway server, e.g.::

    docker run -d --name pocketsync-mongo-it -h pocketsync-mongo-it \\
      -p 127.0.0.1:27039:27017 \\
      -e MONGODB_INITDB_ROOT_USERNAME=<user> -e MONGODB_INITDB_ROOT_PASSWORD=<pw> \\
      mongodb/mongodb-atlas-local:8.0.34
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

from pocketsync import vindex
from pocketsync.model import Recording
from pocketsync.sinks.mongo import MongoSink, connect

pytestmark = pytest.mark.skipif(
    os.environ.get("POCKETSYNC_IT_MONGO") != "1",
    reason="set POCKETSYNC_IT_MONGO=1 (and POCKETSYNC_IT_MONGO_URI) to run against atlas-local",
)

DIMS = 768
MODEL = "synthetic-768"
QUERYABLE_TIMEOUT = 60.0


def _vec(axis: int) -> list[float]:
    """A unit vector along ``axis`` (so cosine neighbours are unambiguous)."""
    v = [0.0] * DIMS
    v[axis] = 1.0
    return v


@pytest.fixture(scope="module")
def sink():
    uri = os.environ.get("POCKETSYNC_IT_MONGO_URI")
    if not uri:
        pytest.fail("POCKETSYNC_IT_MONGO_URI is required when POCKETSYNC_IT_MONGO=1")
    dbname = f"pocketsync_it_{uuid.uuid4().hex[:8]}"
    db = connect(uri, dbname, serverSelectionTimeoutMS=15000)
    db.client.admin.command("ping")
    s = MongoSink(db)
    s.ensure_indexes()
    # (recording id, label, summary axis, chunk axes)
    rows = [("it-1", "alpha", 0, [10, 11]), ("it-2", "beta", 1, [20]), ("it-3", "alpha", 2, [30])]
    for rid, label, axis, chunk_axes in rows:
        rec = Recording(id=rid, title=f"Synthetic {rid}", summary=f"summary {rid}")
        s.put_recording(rec)
        ok = s.set_enrichment(
            rid,
            rec.content_hash(),
            {
                "status": "done",
                "model": MODEL,
                "dims": DIMS,
                "labels": [label],
                "summary_vector": _vec(axis),
                "chunks": [{"text": f"chunk {a}", "vector": _vec(a)} for a in chunk_axes],
            },
        )
        assert ok
    vindex.ensure_index(s.collection, MODEL, DIMS)
    try:
        yield s
    finally:
        db.client.drop_database(dbname)
        db.client.close()


def _wait_queryable(collection, name: str) -> None:
    deadline = time.monotonic() + QUERYABLE_TIMEOUT
    last = None
    while time.monotonic() < deadline:
        for idx in collection.list_search_indexes(name):
            last = idx
            if idx.get("queryable"):
                return
            if idx.get("status") == "FAILED":
                pytest.fail(f"index {name} FAILED: {idx}")
        time.sleep(1)
    pytest.fail(f"index {name} not queryable within {QUERYABLE_TIMEOUT}s; last={last}")


def _search(collection, index: str, path: str, qvec: list[float], flt: dict | None):
    stage = {
        "index": index,
        "path": path,
        "queryVector": qvec,
        "numCandidates": 50,
        "limit": 5,
    }
    if flt:
        stage["filter"] = flt
    return list(
        collection.aggregate(
            [
                {"$vectorSearch": stage},
                {"$project": {"_id": 1, "score": {"$meta": "vectorSearchScore"}}},
            ]
        )
    )


def _eventually(fn, timeout: float = 30.0):
    """Index queryable != caught up with every write; retry until non-empty."""
    deadline = time.monotonic() + timeout
    out = fn()
    while not out and time.monotonic() < deadline:
        time.sleep(1)
        out = fn()
    return out


def test_ensure_index_is_idempotent(sink: MongoSink) -> None:
    assert vindex.ensure_index(sink.collection, MODEL, DIMS) == []
    names = {i["name"] for i in sink.collection.list_search_indexes()}
    assert {vindex.SUMMARY_INDEX, vindex.CHUNK_INDEX} <= names


def test_summary_vector_search_with_label_filter(sink: MongoSink) -> None:
    col = sink.collection
    _wait_queryable(col, vindex.SUMMARY_INDEX)
    query = _vec(0)
    unfiltered = _eventually(
        lambda: _search(col, vindex.SUMMARY_INDEX, vindex.PATH_SUMMARY_VECTOR, query, None)
    )
    assert unfiltered and unfiltered[0]["_id"] == "it-1"
    flt = {vindex.PATH_LABELS: "beta"}
    hits = _eventually(
        lambda: _search(col, vindex.SUMMARY_INDEX, vindex.PATH_SUMMARY_VECTOR, query, flt)
    )
    assert [h["_id"] for h in hits] == ["it-2"]  # the type filter excludes the nearest (alpha)
    flt = {vindex.PATH_LABELS: "alpha"}
    hits = _search(col, vindex.SUMMARY_INDEX, vindex.PATH_SUMMARY_VECTOR, query, flt)
    assert {h["_id"] for h in hits} == {"it-1", "it-3"}


def test_chunk_vector_search_nested_root(sink: MongoSink) -> None:
    """nestedRoot is accepted by atlas-local 8.0.34 and unfiltered chunk search works."""
    col = sink.collection
    _wait_queryable(col, vindex.CHUNK_INDEX)
    hits = _eventually(
        lambda: _search(col, vindex.CHUNK_INDEX, vindex.PATH_CHUNK_VECTOR, _vec(30), None)
    )
    assert hits and hits[0]["_id"] == "it-3"


@pytest.mark.xfail(
    strict=False,
    reason=(
        "FINDING (atlas-local 8.0.34): the nestedRoot chunk index builds and is queryable, but "
        "a $vectorSearch filter on a PARENT-level path (enrichment.labels, declared as a filter "
        "field in the index) silently returns [] instead of an error; filtering on a nested "
        "path (enrichment.chunks.text) errors with \"Path 'enrichment.chunks.text' needs to be "
        'indexed as filter". Chunk-level type filtering needs a vindex.py redesign.'
    ),
)
def test_chunk_vector_search_with_parent_label_filter(sink: MongoSink) -> None:
    col = sink.collection
    _wait_queryable(col, vindex.CHUNK_INDEX)
    hits = _search(
        col, vindex.CHUNK_INDEX, vindex.PATH_CHUNK_VECTOR, _vec(30), {vindex.PATH_LABELS: "alpha"}
    )
    assert hits and hits[0]["_id"] == "it-3"
