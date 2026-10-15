"""Sync-state store: watermark, dedupe, runs, errors, counters; Mongo copy survives restart."""

from __future__ import annotations

import mongomock
import pytest

from pocketsync.state import (
    COUNTER_WEBHOOK_MISSED,
    DEDUPE_COLLECTION,
    STATE_COLLECTION,
    MemoryStateStore,
    MongoStateStore,
    StateStore,
)


@pytest.fixture
def db():
    return mongomock.MongoClient()["pocketsync"]


@pytest.fixture(params=["memory", "mongo"])
def store(request, db):
    if request.param == "memory":
        return MemoryStateStore(max_errors=3)
    return MongoStateStore(db, max_errors=3)


def test_is_state_store(store):
    assert isinstance(store, StateStore)


def test_watermark(store):
    assert store.get_watermark() is None
    store.set_watermark("2026-05-01T00:00:00Z")
    assert store.get_watermark() == "2026-05-01T00:00:00Z"


def test_dedupe(store):
    assert not store.seen("evt-1")
    assert store.mark_seen("evt-1") is True
    assert store.seen("evt-1")
    assert store.mark_seen("evt-1") is False
    assert not store.seen("evt-2")


def test_runs(store):
    assert store.last_run() is None
    store.record_run("2026-05-01T00:00:00Z", "2026-05-01T00:01:00Z", {"changed": 2})
    store.record_run("2026-05-02T00:00:00Z", "2026-05-02T00:01:00Z", {"changed": 0})
    assert store.last_run() == {
        "started": "2026-05-02T00:00:00Z",
        "finished": "2026-05-02T00:01:00Z",
        "stats": {"changed": 0},
    }


def test_errors_bounded(store):
    assert store.errors() == []
    for i in range(5):
        store.record_error("sync", f"e{i}", when=f"2026-05-01T00:00:0{i}Z")
    errs = store.errors()
    assert [e["message"] for e in errs] == ["e2", "e3", "e4"]
    assert errs[-1] == {"where": "sync", "message": "e4", "at": "2026-05-01T00:00:04Z"}


def test_counters(store):
    assert store.counters() == {}
    assert store.increment(COUNTER_WEBHOOK_MISSED) == 1
    assert store.increment(COUNTER_WEBHOOK_MISSED, 2) == 3
    assert store.counters() == {COUNTER_WEBHOOK_MISSED: 3}
    assert store.get_last_webhook_at() is None
    store.set_last_webhook_at("2026-05-01T00:00:00Z")
    assert store.get_last_webhook_at() == "2026-05-01T00:00:00Z"


def test_mongo_state_survives_restart(db):
    s1 = MongoStateStore(db)
    s1.ensure_indexes()
    s1.set_watermark("2026-05-01T00:00:00Z")
    s1.mark_seen("evt-1")
    s1.record_run("a", "b", {"n": 1})
    s1.record_error("webhook", "bad sig", when="t")
    s1.increment(COUNTER_WEBHOOK_MISSED)
    s1.set_last_webhook_at("2026-05-01T00:00:00Z")

    s2 = MongoStateStore(db)  # fresh process, same database
    assert s2.get_watermark() == "2026-05-01T00:00:00Z"
    assert s2.seen("evt-1")
    assert s2.mark_seen("evt-1") is False
    assert s2.last_run() == {"started": "a", "finished": "b", "stats": {"n": 1}}
    assert s2.errors()[-1]["message"] == "bad sig"
    assert s2.counters() == {COUNTER_WEBHOOK_MISSED: 1}
    assert s2.get_last_webhook_at() == "2026-05-01T00:00:00Z"


def test_mongo_layout(db):
    s = MongoStateStore(db)
    s.ensure_indexes()
    s.set_watermark("w")
    s.mark_seen("k")
    assert db[STATE_COLLECTION].count_documents({}) == 1
    assert db[DEDUPE_COLLECTION].find_one({"_id": "k"}) is not None
    s.ensure_indexes()  # idempotent
