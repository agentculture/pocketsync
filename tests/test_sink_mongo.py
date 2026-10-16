"""MongoSink: one doc per recording, field groups, CAS enrichment, tombstones (mongomock)."""

from __future__ import annotations

import itertools
import subprocess
import sys

import mongomock
import pytest

from pocketsync import vindex
from pocketsync.cli._errors import EXIT_ENV_ERROR, CliError
from pocketsync.model import ActionItem, Recording, TranscriptSegment
from pocketsync.sinks.base import FileRef, FileSet, Sink
from pocketsync.sinks.mongo import ENRICHMENT_PENDING, MongoSink


def clock():
    counter = itertools.count(1)
    return lambda: f"2026-10-14T00:00:{next(counter):02d}Z"


@pytest.fixture
def db():
    return mongomock.MongoClient()["pocketsync"]


@pytest.fixture
def sink(db):
    return MongoSink(db, now=clock())


def rec(rid="r1", summary="s", **kw):
    return Recording(
        id=rid,
        title="Title",
        recording_at="2026-05-01T10:00:00Z",
        updated_at="2026-05-01T11:00:00Z",
        created_at="2026-05-01T10:59:00Z",
        summary=summary,
        tags=["t"],
        speakers=["A"],
        segments=[TranscriptSegment(start=0, end=1, text="hi", speaker="A")],
        action_items=[ActionItem(text="do it")],
        raw={"wire": "x" * 10},
        **kw,
    )


def doc(db, rid="r1"):
    return db[vindex.COLLECTION].find_one({"_id": rid})


def test_is_sink(sink):
    assert isinstance(sink, Sink)


def test_one_doc_per_recording_with_content_fields(sink, db):
    r = rec()
    files = FileSet(summary_md=FileRef("2026/05/r1/summary.md", 3, "ab"))
    res = sink.put_recording(r, files)
    assert res.changed
    assert res.content_hash == r.content_hash()
    assert db[vindex.COLLECTION].count_documents({}) == 1
    d = doc(db)
    assert d["title"] == "Title"
    assert d[vindex.PATH_RECORDING_AT] == "2026-05-01T10:00:00Z"
    assert d["segments"][0]["text"] == "hi"
    assert d["action_items"][0]["text"] == "do it"
    assert d["content_hash"] == r.content_hash()
    assert d["files"]["summary_md"]["rel_path"] == "2026/05/r1/summary.md"
    assert d["landed_at"] == d["updated_at"]
    assert d[vindex.PATH_DELETED] is False
    assert d["merged_into"] is None
    assert d["enrichment"]["status"] == ENRICHMENT_PENDING
    # Pocket's own timestamps live under source; raw wire data is not stored.
    assert d["source"] == {"created_at": "2026-05-01T10:59:00Z", "updated_at": r.updated_at}
    assert "raw" not in d
    assert "fetched_at" not in d


def test_identical_redelivery_is_noop(sink, db):
    sink.put_recording(rec())
    before = doc(db)
    res = sink.put_recording(rec(fetched_at="2026-10-14T09:00:00Z"))
    assert not res.changed
    after = doc(db)
    assert after["updated_at"] == before["updated_at"]
    assert after["content_hash"] == before["content_hash"]
    assert after == before


def test_content_change_resets_enrichment(sink, db):
    sink.put_recording(rec())
    h1 = doc(db)["content_hash"]
    assert sink.set_enrichment(
        "r1",
        h1,
        {
            "status": "done",
            "model": "m",
            "dims": 2,
            "labels": ["x"],
            "summary_vector": [0.1, 0.2],
            "chunks": [{"index": 0, "vector": [1.0, 0.0]}],
        },
    )
    first = doc(db)
    res = sink.put_recording(rec(summary="new summary"))
    assert res.changed
    d = doc(db)
    assert d["summary"] == "new summary"
    assert d["content_hash"] != h1
    assert d["updated_at"] != first["updated_at"]
    assert d["landed_at"] == first["landed_at"]
    enr = d["enrichment"]
    assert enr["status"] == ENRICHMENT_PENDING
    for stale in ("summary_vector", "chunks", "labels", "model", "dims"):
        assert stale not in enr


def test_content_change_keeps_tombstone_and_merge(sink, db):
    sink.put_recording(rec())
    sink.tombstone("r1", "2026-06-01T00:00:00Z")
    sink.mark_merged("r1", "r0", "2026-06-02T00:00:00Z")
    sink.put_recording(rec(summary="changed"))
    st = sink.get_state("r1")
    assert st.deleted_in_pocket
    assert st.merged_into == "r0"


def test_set_enrichment_cas(sink, db):
    sink.put_recording(rec())
    h = doc(db)["content_hash"]
    assert sink.set_enrichment("r1", "not-the-hash", {"status": "done", "model": "m"}) is False
    assert "model" not in doc(db)["enrichment"]
    assert sink.set_enrichment("r1", h, {"status": "done", "model": "m", "dims": 2}) is True
    d = doc(db)
    assert d["enrichment"]["model"] == "m"
    assert d["enrichment"]["status"] == "done"
    assert sink.set_enrichment("missing", h, {"status": "done"}) is False


def test_set_enrichment_writes_only_enrichment_fields(sink, db):
    sink.put_recording(rec())
    before = doc(db)
    h = before["content_hash"]
    assert sink.set_enrichment("r1", h, {"status": "done", "labels": ["a"]})
    after = doc(db)
    for k in before:
        if k != "enrichment":
            assert after[k] == before[k]
    with pytest.raises(ValueError):
        sink.set_enrichment("r1", h, {"title": "hijack"})
    assert doc(db)["title"] == "Title"


def test_enrichment_race_content_changes_mid_flight(sink, db):
    """Worker reads hash h1, content changes to h2 meanwhile, worker's write must be refused."""
    sink.put_recording(rec())
    (job,) = sink.pending_enrichment(10)
    started_hash = job["content_hash"]
    sink.put_recording(rec(summary="re-processed by Pocket"))
    ok = sink.set_enrichment(
        "r1", started_hash, {"status": "done", "summary_vector": [1.0], "model": "m"}
    )
    assert ok is False
    enr = doc(db)["enrichment"]
    assert enr["status"] == ENRICHMENT_PENDING
    assert "summary_vector" not in enr


def test_set_enrichment_error_counts_attempts(sink, db):
    sink.put_recording(rec())
    h = doc(db)["content_hash"]
    assert sink.set_enrichment_error("r1", h, "boom")
    assert sink.set_enrichment_error("r1", h, "boom again")
    enr = doc(db)["enrichment"]
    assert enr["status"] == "error"
    assert enr["error"] == "boom again"
    assert enr["attempts"] == 2
    assert sink.set_enrichment_error("r1", "stale", "x") is False


def test_pending_enrichment_excludes_deleted_and_done(sink, db):
    for rid in ("a", "b", "c"):
        sink.put_recording(rec(rid))
    sink.tombstone("b", "2026-06-01T00:00:00Z")
    sink.set_enrichment("c", doc(db, "c")["content_hash"], {"status": "done"})
    assert [d["_id"] for d in sink.pending_enrichment(10)] == ["a"]
    assert sink.pending_enrichment(0) == []


def test_tombstone_and_merge_never_delete(sink, db):
    sink.put_recording(rec("r1"))
    sink.put_recording(rec("r2"))
    sink.tombstone("r1", "2026-06-01T00:00:00Z")
    sink.mark_merged("r2", "r1", "2026-06-02T00:00:00Z")
    d1, d2 = doc(db, "r1"), doc(db, "r2")
    assert d1[vindex.PATH_DELETED] is True
    assert d1["deleted_at"] == "2026-06-01T00:00:00Z"
    assert d2["merged_into"] == "r1"
    assert d2["merged_at"] == "2026-06-02T00:00:00Z"
    assert d1["title"] == "Title"
    assert d2["segments"]
    assert db[vindex.COLLECTION].count_documents({}) == 2


def test_tombstone_unknown_raises(sink):
    with pytest.raises(CliError):
        sink.tombstone("nope", "2026-06-01T00:00:00Z")
    with pytest.raises(CliError):
        sink.mark_merged("nope", "x", "2026-06-01T00:00:00Z")


def test_dry_run_writes_nothing(sink, db):
    assert sink.put_recording(rec(), dry_run=True).changed
    assert db[vindex.COLLECTION].count_documents({}) == 0
    sink.put_recording(rec())
    assert not sink.put_recording(rec(), dry_run=True).changed
    assert sink.put_recording(rec(summary="z"), dry_run=True).changed
    assert doc(db)["summary"] == "s"


def test_files_update_without_content_change(sink, db):
    sink.put_recording(rec(), FileSet(summary_md=FileRef("a/summary.md", 1, "aa")))
    before = doc(db)
    fs = FileSet(summary_md=FileRef("a/summary.md", 1, "aa"), audio=FileRef("a/audio.m4a", 9, "bb"))
    res = sink.put_recording(rec(), fs)
    assert not res.changed
    after = doc(db)
    assert after["files"]["audio"]["rel_path"] == "a/audio.m4a"
    assert after["updated_at"] == before["updated_at"]
    assert res.files.audio.rel_path == "a/audio.m4a"


def test_audio_url_ignored(sink, db):
    sink.put_recording(rec(), audio_url="https://example.invalid/a.m4a")
    assert doc(db)["files"] == FileSet().to_dict()


def test_lost_upsert_race_is_noop(sink, db):
    """Another writer lands the same hash between our read and upsert -> no error, no change."""
    r = rec()
    db[vindex.COLLECTION].insert_one({"_id": "r1", "content_hash": r.content_hash()})
    assert sink.put_recording(r).changed is False


def test_ensure_indexes(sink, db):
    sink.ensure_indexes()
    keys = {tuple(v["key"])[0][0] for v in db[vindex.COLLECTION].index_information().values()}
    assert {"enrichment.status", vindex.PATH_DELETED, vindex.PATH_RECORDING_AT} <= keys
    sink.ensure_indexes()  # idempotent


def test_repr_hides_connection(sink):
    assert "mongodb" not in repr(sink)


def test_from_uri_uses_pymongo_lazily(monkeypatch):
    import pymongo

    seen = {}

    class FakeClient:
        def __init__(self, uri, **kw):
            seen["uri"] = uri

        def __getitem__(self, name):
            seen["db"] = name
            return mongomock.MongoClient()[name]

    monkeypatch.setattr(pymongo, "MongoClient", FakeClient)
    s = MongoSink.from_uri("mongodb://u:p@localhost:27017/", db="ps")
    assert seen == {"uri": "mongodb://u:p@localhost:27017/", "db": "ps"}
    assert "u:p" not in repr(s)


def test_import_without_pymongo():
    code = (
        "import sys; sys.modules['pymongo']=None\n"
        "import pocketsync, pocketsync.sinks.local, pocketsync.state\n"
        "from pocketsync.sinks.mongo import MongoSink\n"
        "from pocketsync.cli._errors import CliError\n"
        "try:\n"
        "    MongoSink.from_uri('mongodb://localhost:27017/')\n"
        "except CliError as e:\n"
        f"    assert e.code == {EXIT_ENV_ERROR} and 'pocketsync[mongo]' in e.remediation\n"
        "    assert 'localhost' not in e.message\n"
        "else:\n"
        "    raise SystemExit(1)\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_content_change_without_files_keeps_refs(sink, db):
    sink.put_recording(rec(), FileSet(audio=FileRef("a/audio.m4a", 9, "bb")))
    sink.put_recording(rec(summary="changed"))
    assert doc(db)["files"]["audio"]["rel_path"] == "a/audio.m4a"


# -- requeue / generation CAS / persisted deferral (talk-to-recordings t4) ----------------


def enr(db, rid="r1"):
    return doc(db, rid)["enrichment"]


def test_requeue_resets_status_and_bumps_generation(sink, db):
    sink.put_recording(rec())
    h = doc(db)["content_hash"]
    sink.set_enrichment_error("r1", h, "boom", next_attempt_at=99.0)
    db[vindex.COLLECTION].update_one({"_id": "r1"}, {"$set": {"enrichment.deferred_reason": "x"}})
    assert sink.requeue(["r1"], "classify") == 1
    e = enr(db)
    assert e["status"] == ENRICHMENT_PENDING
    assert e["attempts"] == 0
    assert e["mode"] == "classify"
    assert e["generation"] == 1
    for gone in ("error", "next_attempt_at", "deferred_reason"):
        assert gone not in e
    assert sink.requeue(["r1"], "embed") == 1
    assert enr(db)["generation"] == 2
    assert enr(db)["mode"] == "embed"


def test_requeue_skips_deleted_and_unknown_and_counts(sink, db):
    for rid in ("a", "b", "c"):
        sink.put_recording(rec(rid))
    sink.tombstone("b", "2026-06-01T00:00:00Z")
    assert sink.requeue(["a", "b", "c", "nope"], "full") == 2
    assert "generation" not in enr(db, "b")
    assert enr(db, "b").get("mode") is None
    assert sink.requeue([], "full") == 0


def test_requeue_rejects_unknown_mode(sink, db):
    sink.put_recording(rec())
    with pytest.raises(ValueError):
        sink.requeue(["r1"], "bogus")
    assert "generation" not in enr(db)


def test_requeue_mid_flight_is_not_overwritten(sink, db):
    """c33: claim a doc, requeue it, finish the original run -> doc stays pending."""
    sink.put_recording(rec())
    (job,) = sink.pending_enrichment(10)
    gen = job["enrichment"]["generation"]
    assert gen == 0  # docs never requeued report generation 0
    sink.requeue(["r1"], "full")
    ok = sink.set_enrichment(
        "r1", job["content_hash"], {"status": "done", "model": "m"}, expected_generation=gen
    )
    assert ok is False
    assert enr(db)["status"] == ENRICHMENT_PENDING
    assert "model" not in enr(db)
    assert (
        sink.set_enrichment_error("r1", job["content_hash"], "boom", expected_generation=gen)
        is False
    )
    assert enr(db)["status"] == ENRICHMENT_PENDING
    # re-processed on the next cycle, with the new generation
    (job2,) = sink.pending_enrichment(10)
    assert job2["enrichment"]["generation"] == 1
    assert sink.set_enrichment(
        "r1", job2["content_hash"], {"status": "done"}, expected_generation=1
    )
    assert enr(db)["status"] == "done"


def test_absent_generation_matches_docs_without_one(sink, db):
    sink.put_recording(rec())
    h = doc(db)["content_hash"]
    assert "generation" not in enr(db)
    assert sink.set_enrichment("r1", h, {"status": "done"}, expected_generation=0) is True
    assert sink.set_enrichment("r1", h, {"status": "done"}, expected_generation=1) is False
    assert sink.set_enrichment_error("r1", h, "x", expected_generation=0) is True
    assert sink.set_enrichment_error("r1", h, "x", expected_generation=3) is False


def test_expected_generation_default_does_not_check(sink, db):
    """The pre-generation worker calls (no expected_generation) keep working."""
    sink.put_recording(rec())
    h = doc(db)["content_hash"]
    sink.requeue(["r1"], "full")
    sink.requeue(["r1"], "full")
    assert sink.set_enrichment_error("r1", h, "boom")
    assert sink.set_enrichment("r1", h, {"status": "done"})


def test_set_deferred_persists_reason_and_set_enrichment_clears(sink, db):
    sink.put_recording(rec())
    h = doc(db)["content_hash"]
    assert sink.set_deferred("r1", h, 0, "gateway 503", 1000.0) is True
    e = enr(db)
    assert e["status"] == ENRICHMENT_PENDING
    assert e["deferred_reason"] == "gateway 503"
    assert e["next_attempt_at"] == 1000.0
    assert sink.set_deferred("r1", "stale", 0, "x", 1.0) is False
    assert sink.set_deferred("r1", h, 7, "x", 1.0) is False
    assert sink.set_enrichment("r1", h, {"status": "done"}, expected_generation=0)
    e = enr(db)
    assert "deferred_reason" not in e and "next_attempt_at" not in e
    # only a pending doc can be deferred
    assert sink.set_deferred("r1", h, 0, "x", 1.0) is False


def test_set_enrichment_error_clears_deferral_reason(sink, db):
    sink.put_recording(rec())
    h = doc(db)["content_hash"]
    sink.set_deferred("r1", h, 0, "gateway 503", 1000.0)
    assert sink.set_enrichment_error("r1", h, "boom", next_attempt_at=2000.0)
    e = enr(db)
    assert "deferred_reason" not in e
    assert e["next_attempt_at"] == 2000.0


def test_pending_enrichment_honours_persisted_deferral(sink, db):
    for rid in ("a", "b", "c"):
        sink.put_recording(rec(rid))
    sink.set_deferred("a", doc(db, "a")["content_hash"], 0, "503", 500.0)
    sink.set_deferred("b", doc(db, "b")["content_hash"], 0, "503", 50.0)
    assert [d["_id"] for d in sink.pending_enrichment(10, now=100.0)] == ["b", "c"]
    assert [d["_id"] for d in sink.pending_enrichment(10, now=600.0)] == ["a", "b", "c"]
    # without a clock, today's behaviour: every pending doc
    assert [d["_id"] for d in sink.pending_enrichment(10)] == ["a", "b", "c"]


def test_pending_enrichment_returns_generation(sink, db):
    sink.put_recording(rec("a"))
    sink.put_recording(rec("b"))
    sink.requeue(["b"], "embed")
    got = {d["_id"]: d["enrichment"]["generation"] for d in sink.pending_enrichment(10)}
    assert got == {"a": 0, "b": 1}


def test_enrichment_fields_whitelist_grew():
    from pocketsync.sinks.mongo import ENRICHMENT_FIELDS

    assert {"mode", "generation", "deferred_reason"} <= ENRICHMENT_FIELDS


def test_set_enrichment_accepts_mode_payload(sink, db):
    sink.put_recording(rec())
    h = doc(db)["content_hash"]
    assert sink.set_enrichment("r1", h, {"status": "done", "mode": "full"})
    assert enr(db)["mode"] == "full"


def test_content_change_resets_requeue_and_deferral_state(sink, db):
    sink.put_recording(rec())
    h = doc(db)["content_hash"]
    sink.requeue(["r1"], "classify")
    sink.set_deferred("r1", h, 1, "503", 1000.0)
    sink.put_recording(rec(summary="re-processed"))
    e = enr(db)
    assert e["status"] == ENRICHMENT_PENDING and e["attempts"] == 0
    for gone in ("mode", "deferred_reason", "next_attempt_at"):
        assert gone not in e
    # generation is monotonic: a content change never rewinds it (no ABA on the CAS)
    assert e["generation"] == 1
