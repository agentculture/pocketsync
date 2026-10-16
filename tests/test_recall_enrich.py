"""Re-enrich selection: plan_enrich selects without writing; apply requeues (mongomock)."""

from __future__ import annotations

import mongomock
import pytest

from pocketsync import vindex
from pocketsync.cli._errors import CliError
from pocketsync.recall.enrich import plan_enrich
from pocketsync.sinks.mongo import MongoSink


@pytest.fixture
def db():
    return mongomock.MongoClient()["pocketsync"]


@pytest.fixture
def store(db):
    s = MongoSink(db)
    col = db[vindex.COLLECTION]
    rows = [
        ("a", "2026-01-05T10:00:00Z", ["meeting"], "done", False),
        ("b", "2026-02-10T10:00:00Z", ["idea", "meeting"], "error", False),
        ("c", "2026-03-15T23:59:00Z", ["idea"], "pending", False),
        ("d", "2026-03-20T10:00:00Z", ["meeting"], "done", True),  # deleted
    ]
    for rid, at, labels, status, deleted in rows:
        col.insert_one(
            {
                "_id": rid,
                "recording_at": at,
                "deleted_in_pocket": deleted,
                "enrichment": {"labels": labels, "status": status, "generation": 0},
            }
        )
    return s


def snapshot(db):
    return sorted(db[vindex.COLLECTION].find(), key=lambda d: d["_id"])


def ids(res):
    return sorted(res["ids"])


def test_no_selector_refused(store):
    with pytest.raises(CliError) as e:
        plan_enrich(store)
    assert e.value.code == 1 and e.value.remediation


def test_all_skips_deleted(store):
    res = plan_enrich(store, all=True)
    assert ids(res) == ["a", "b", "c"]
    assert res["count"] == 3 and res["dry_run"] is True and res["updated"] == 0


def test_since_until(store):
    assert ids(plan_enrich(store, since="2026-02-01")) == ["b", "c"]
    assert ids(plan_enrich(store, until="2026-02-10")) == ["a", "b"]
    assert ids(plan_enrich(store, since="2026-02-01", until="2026-03-15")) == ["b", "c"]
    assert ids(plan_enrich(store, since="2026-02-10T10:00:01Z")) == ["c"]


def test_bad_date(store):
    with pytest.raises(CliError):
        plan_enrich(store, since="yesterday")


def test_ids_selector(store):
    assert ids(plan_enrich(store, ids=["a", "d", "zzz"])) == ["a"]


def test_label(store):
    assert ids(plan_enrich(store, label="meeting")) == ["a", "b"]
    assert ids(plan_enrich(store, label="idea")) == ["b", "c"]


def test_status(store):
    assert ids(plan_enrich(store, status="error")) == ["b"]
    assert ids(plan_enrich(store, status="done")) == ["a"]


def test_selectors_combine(store):
    assert ids(plan_enrich(store, label="meeting", status="error")) == ["b"]


def test_bad_status_and_only(store):
    with pytest.raises(CliError):
        plan_enrich(store, status="weird")
    with pytest.raises(CliError):
        plan_enrich(store, all=True, only="nope")


def test_estimates(store):
    assert plan_enrich(store, all=True)["estimated_gateway_calls"] == 6
    assert plan_enrich(store, all=True, only="all")["mode"] == "full"
    r = plan_enrich(store, all=True, only="classify")
    assert r["mode"] == "classify" and r["estimated_gateway_calls"] == 3
    r = plan_enrich(store, all=True, only="embed")
    assert r["mode"] == "embed" and r["estimated_gateway_calls"] == 3


def test_dry_run_changes_nothing(store, db):
    before = snapshot(db)
    plan_enrich(store, all=True)
    plan_enrich(store, all=True, apply=False, only="embed")
    assert snapshot(db) == before


def test_apply_requeues(store, db):
    res = plan_enrich(store, label="meeting", only="classify", apply=True)
    assert res["dry_run"] is False and res["updated"] == 2
    col = db[vindex.COLLECTION]
    for rid in ("a", "b"):
        e = col.find_one({"_id": rid})["enrichment"]
        assert e["status"] == "pending" and e["mode"] == "classify" and e["generation"] == 1
    assert col.find_one({"_id": "c"})["enrichment"]["status"] == "pending"
    assert "mode" not in col.find_one({"_id": "c"})["enrichment"]
    assert col.find_one({"_id": "d"})["enrichment"]["status"] == "done"


def test_apply_nothing_selected(store):
    res = plan_enrich(store, label="nolabel", apply=True)
    assert res["count"] == 0 and res["updated"] == 0
