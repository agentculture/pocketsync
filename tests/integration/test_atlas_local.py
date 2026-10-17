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
from pocketsync.recall.search import search as recall_search
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
    # Recall-search rows (label "gamma"): (id, sensitive score, recording_at, axis, fate)
    recall_rows = [
        ("it-4", 0.9, "2026-10-10T10:00:00Z", 40, None),
        ("it-5", None, "2026-10-10T11:00:00Z", 40, "deleted"),
        ("it-6", None, "2026-10-10T12:00:00Z", 40, "merged"),
        ("it-7", None, "2026-10-12T10:00:00Z", 41, None),
    ]
    for rid, sens, at, axis, fate in recall_rows:
        rec = Recording(id=rid, title=f"Synthetic {rid}", summary=f"summary {rid}", recording_at=at)
        s.put_recording(rec)
        chunk = {"index": 0, "start": 1.0, "end": 2.0, "text": f"chunk {rid}", "vector": _vec(axis)}
        assert s.set_enrichment(
            rid,
            rec.content_hash(),
            {
                "status": "done",
                "model": MODEL,
                "dims": DIMS,
                "labels": ["gamma"],
                "filters": {} if sens is None else {"sensitive": sens},
                "summary_vector": _vec(axis),
                "chunks": [chunk],
            },
        )
        if fate == "deleted":
            s.tombstone(rid, "2026-10-11T00:00:00Z")
        elif fate == "merged":
            s.mark_merged(rid, "it-4", "2026-10-11T00:00:00Z")
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


def _search(
    collection,
    index: str,
    path: str,
    qvec: list[float],
    flt: dict | None,
    *,
    filter_key: str = "filter",
):
    """``filter_key`` is ``parentFilter`` for root-level paths on a nestedRoot index."""
    stage = {
        "index": index,
        "path": path,
        "queryVector": qvec,
        "numCandidates": 50,
        "limit": 5,
    }
    if flt:
        stage[filter_key] = flt
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
    assert unfiltered
    assert unfiltered[0]["_id"] == "it-1"
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
    assert hits
    assert hits[0]["_id"] == "it-3"


def test_chunk_vector_search_with_parent_label_filter(sink: MongoSink) -> None:
    """Root-level filters on a nestedRoot index go in ``parentFilter``.

    Verified on atlas-local 8.0.34: the same predicate under ``filter`` silently returns [].
    """
    col = sink.collection
    _wait_queryable(col, vindex.CHUNK_INDEX)
    flt = {vindex.PATH_LABELS: "alpha"}
    hits = _eventually(
        lambda: _search(
            col,
            vindex.CHUNK_INDEX,
            vindex.PATH_CHUNK_VECTOR,
            _vec(30),
            flt,
            filter_key="parentFilter",
        )
    )
    assert hits
    assert hits[0]["_id"] == "it-3"


class _QueryGateway:
    """Embeds every query as one fixed synthetic vector and records the prompt names."""

    def __init__(self, vec: list[float]) -> None:
        self.vec = vec
        self.prompts: set[str] = set()

    def embed(self, texts, prompt_name="Document"):
        self.prompts.add(prompt_name)
        return [list(self.vec) for _ in texts]


def _ids_eventually(fn, expected: set[str], timeout: float = 30.0) -> set[str]:
    deadline = time.monotonic() + timeout
    ids = {r["id"] for r in fn()["results"]}
    while ids != expected and time.monotonic() < deadline:
        time.sleep(1)
        ids = {r["id"] for r in fn()["results"]}
    return ids


def test_recall_search_filters_on_real_vector_search(sink: MongoSink) -> None:
    """pocketsync.recall.search against real $vectorSearch: parentFilter + post-$match.

    Deleted (it-5) and merged (it-6) docs never appear; sensitive (it-4) is returned by
    default and dropped only with exclude_sensitive; the recording_at range applies.
    """
    col = sink.collection
    _wait_queryable(col, vindex.CHUNK_INDEX)
    _wait_queryable(col, vindex.SUMMARY_INDEX)
    gw = _QueryGateway(_vec(40))

    def run(**kw):
        return recall_search(sink, gw, "synthetic", labels=["gamma"], limit=5, **kw)

    assert _ids_eventually(run, {"it-4", "it-7"}) == {"it-4", "it-7"}
    out = run()
    assert out["mode"] == "vector"
    assert out["results"][0]["id"] == "it-4"
    assert out["results"][0]["chunks"][0]["start"] == 1.0
    assert gw.prompts == {"query"}
    assert {r["id"] for r in run(exclude_sensitive=True)["results"]} == {"it-7"}
    assert {r["id"] for r in run(since="2026-10-11")["results"]} == {"it-7"}
    assert {r["id"] for r in run(until="2026-10-10")["results"]} == {"it-4"}


def test_stale_vector_index_gains_new_filter_fields(sink):
    # atlas-local rejects updateSearchIndex for vectorSearch; ensure_index re-creates instead.
    from pymongo.operations import SearchIndexModel

    col = sink.collection.database["stale_index_probe"]
    col.insert_one({"_id": "p1"})
    stale = vindex.summary_definition(DIMS)
    stale["fields"] = [f for f in stale["fields"] if f["path"] != vindex.PATH_SENSITIVE]
    col.create_search_index(
        SearchIndexModel(definition=stale, name=vindex.SUMMARY_INDEX, type="vectorSearch")
    )
    _wait_queryable(col, vindex.SUMMARY_INDEX)
    vindex.ensure_index(col, MODEL, DIMS)
    _wait_queryable(col, vindex.SUMMARY_INDEX)
    (idx,) = list(col.list_search_indexes(vindex.SUMMARY_INDEX))
    paths = {f["path"] for f in idx["latestDefinition"]["fields"]}
    assert vindex.PATH_SENSITIVE in paths
