"""`pocketsync status` (read-only health) and `pocketsync reembed` (dry-run unless --apply)."""

from __future__ import annotations

import json

import pytest

from pocketsync.cli import main
from pocketsync.enrich.gateway import EMBED_DIMS, EMBED_MODEL
from pocketsync.state import COUNTER_WEBHOOK_MISSED
from pocketsync.sync import land
from tests.cli_fakes import FakeRuntime, install, rec


@pytest.fixture
def rt(tmp_path, monkeypatch):
    return install(monkeypatch, FakeRuntime(tmp_path, [rec(1), rec(2), rec(3)]))


def test_status_json_reports_every_health_field(rt, capsys):
    for rid in ("r0001", "r0002"):
        land(rid, rt.pocket, files_sink=rt.files, store_sink=rt.store)
    st = rt.counting_state.inner
    st.set_watermark("2026-10-14T00:00:00Z")
    st.record_run("2026-10-14T00:00:00Z", "2026-10-14T00:00:05Z", {"landed": 2})
    st.set_last_webhook_at("2026-10-14T00:01:00+00:00")
    st.increment(COUNTER_WEBHOOK_MISSED)
    st.increment(COUNTER_WEBHOOK_MISSED)
    st.record_error("reconcile", "boom")
    before = rt.total_writes()

    assert main(["status", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["watermark"] == "2026-10-14T00:00:00Z"
    assert out["last_run"]["stats"] == {"landed": 2}
    assert out["last_webhook_at"] == "2026-10-14T00:01:00+00:00"
    assert out["webhook_missed"] == 2
    assert out["enrichment_backlog"] == 2
    assert out["last_errors"][-1]["message"] == "boom"
    assert out["error_count"] == 1
    assert rt.total_writes() == before  # read-only


def test_status_empty_store(rt, capsys):
    assert main(["status", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["watermark"] is None
    assert out["last_run"] is None
    assert out["last_webhook_at"] is None
    assert out["webhook_missed"] == 0
    assert out["enrichment_backlog"] == 0
    assert out["last_errors"] == []


def test_status_text(rt, capsys):
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    for word in ("watermark", "last webhook", "webhook_missed", "enrichment backlog"):
        assert word in out


def test_status_requires_mongo(tmp_path, monkeypatch, capsys):
    install(monkeypatch, FakeRuntime(tmp_path, [], POCKETSYNC_MONGO_URI=""))
    assert main(["status"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


def _seed_mismatched(rt):
    for rid in ("r0001", "r0002", "r0003"):
        land(rid, rt.pocket, files_sink=rt.files, store_sink=rt.store)
    col = rt.db["recordings"]
    col.update_one(
        {"_id": "r0001"},
        {"$set": {"enrichment.status": "done", "enrichment.model": "old", "enrichment.dims": 512}},
    )
    col.update_one(
        {"_id": "r0002"},
        {
            "$set": {
                "enrichment.status": "done",
                "enrichment.model": EMBED_MODEL,
                "enrichment.dims": EMBED_DIMS,
            }
        },
    )
    col.update_one({"_id": "r0003"}, {"$set": {"enrichment.status": "done"}})
    return col


def test_reembed_dry_run_lists_ids_and_writes_nothing(rt, capsys):
    col = _seed_mismatched(rt)
    snapshot = list(col.find({}))
    assert main(["reembed", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "dry_run": True,
        "model": EMBED_MODEL,
        "dims": EMBED_DIMS,
        "ids": ["r0001"],
        "count": 1,
        "updated": 0,
    }
    assert list(col.find({})) == snapshot


def test_reembed_apply_marks_only_affected_pending(rt, capsys):
    col = _seed_mismatched(rt)
    before = {d["_id"]: d for d in col.find({})}
    assert main(["reembed", "--apply", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is False
    assert out["updated"] == 1
    after = {d["_id"]: d for d in col.find({})}
    assert after["r0001"]["enrichment"]["status"] == "pending"
    # nothing else on that doc changed, and the other docs are untouched
    a1 = dict(after["r0001"], enrichment=dict(after["r0001"]["enrichment"], status="done"))
    assert a1 == before["r0001"]
    assert after["r0002"] == before["r0002"]
    assert after["r0003"] == before["r0003"]


def test_reembed_text_dry_run(rt, capsys):
    _seed_mismatched(rt)
    assert main(["reembed"]) == 0
    out = capsys.readouterr().out
    assert "r0001" in out
    assert "--apply" in out


def test_status_and_doctor_report_retrying_and_failed_enrichment(rt, capsys):
    from pocketsync.sinks.mongo import MAX_ENRICHMENT_ATTEMPTS

    for rid in ("r0001", "r0002", "r0003"):
        land(rid, rt.pocket, files_sink=rt.files, store_sink=rt.store)
    col = rt.store.collection
    col.update_one(
        {"_id": "r0001"},
        {
            "$set": {
                "enrichment.status": "error",
                "enrichment.attempts": 2,
                "enrichment.next_attempt_at": 1.0,
            }
        },
    )
    col.update_one(
        {"_id": "r0002"},
        {"$set": {"enrichment.status": "error", "enrichment.attempts": MAX_ENRICHMENT_ATTEMPTS}},
    )
    assert main(["status", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["enrichment_backlog"] == 1
    assert out["enrichment_retrying"] == 1
    assert out["enrichment_failed"] == 1
    assert main(["status"]) == 0
    text = capsys.readouterr().out
    assert "enrichment retrying" in text
    assert "enrichment failed" in text
