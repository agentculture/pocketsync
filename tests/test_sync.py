"""Landing engine: webhook/reconciler parity, audio degradation, dry-run, delete/merge."""

from __future__ import annotations

import io
import itertools

import mongomock
import pytest

from pocketsync.cli._errors import EXIT_ENV_ERROR, CliError
from pocketsync.model import ActionItem, Recording, TranscriptSegment
from pocketsync.pocket.fake import FakePocketSource
from pocketsync.sinks.local import LocalSink
from pocketsync.sinks.mongo import MongoSink
from pocketsync.sync import handle_deleted, handle_merged, land

AUDIO = b"synthetic-audio" * 100


class Resp:
    def __init__(self):
        self._io = io.BytesIO(AUDIO)
        self.headers = {"Content-Type": "audio/mpeg"}

    def read(self, n=-1):
        return self._io.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_rec(rid="r1", **kw):
    base = dict(
        id=rid,
        title="Synthetic",
        recording_at="2026-03-05T10:00:00Z",
        updated_at="2026-03-05T11:00:00Z",
        segments=[TranscriptSegment(start=1, end=2, text="hello", speaker_name="A")],
        summary="A summary.",
        action_items=[ActionItem(text="do it")],
    )
    base.update(kw)
    return Recording(**base)


def clock():
    c = itertools.count(1)
    return lambda: f"2026-10-14T00:00:{next(c):02d}Z"


def stores(tmp_path, name="a"):
    local = LocalSink(tmp_path / name, opener=lambda u: Resp())
    mongo = MongoSink(mongomock.MongoClient()[f"db_{name}"], now=clock())
    return local, mongo


def strip(doc):
    # meta.json embeds a wall-clock synced_at, so its ref (size/sha) legitimately differs.
    out = {k: v for k, v in doc.items() if k not in ("landed_at", "updated_at")}
    out["files"] = {k: v for k, v in doc["files"].items() if k != "meta"}
    return out


def test_land_writes_files_and_doc(tmp_path):
    src = FakePocketSource([make_rec()])
    local, mongo = stores(tmp_path)
    res = land("r1", src, files_sink=local, store_sink=mongo)
    assert res.files_changed
    assert res.store_changed
    assert not res.audio_missing
    doc = mongo.collection.find_one({"_id": "r1"})
    assert doc["files"]["audio"]["size"] == len(AUDIO)
    assert doc["files"]["transcript_md"]["rel_path"].endswith("transcript.md")
    assert doc["content_hash"] == res.content_hash
    assert doc["enrichment"]["status"] == "pending"
    assert src.calls == ["get_recording", "get_audio_url"]


def test_webhook_and_reconciler_paths_identical(tmp_path):
    src = FakePocketSource([make_rec()])
    la, ma = stores(tmp_path, "a")
    lb, mb = stores(tmp_path, "b")
    land("r1", src, files_sink=la, store_sink=ma)  # webhook-shaped: bare id
    rec = src.list_recordings().recordings[0]  # reconciler-shaped: id from a listing
    land(rec.id, src, files_sink=lb, store_sink=mb)
    da = strip(ma.collection.find_one({"_id": "r1"}))
    db = strip(mb.collection.find_one({"_id": "r1"}))
    assert da == db


def test_reland_is_noop(tmp_path):
    src = FakePocketSource([make_rec()])
    local, mongo = stores(tmp_path)
    land("r1", src, files_sink=local, store_sink=mongo)
    before = mongo.collection.find_one({"_id": "r1"})
    res = land("r1", src, files_sink=local, store_sink=mongo)
    assert not res.files_changed
    assert not res.store_changed
    assert mongo.collection.find_one({"_id": "r1"}) == before  # meta ref not clobbered


def test_audio_url_failure_lands_text_and_flags(tmp_path):
    src = FakePocketSource([make_rec()])
    src.get_audio_url = lambda *a, **k: (_ for _ in ()).throw(CliError(EXIT_ENV_ERROR, "boom", "x"))
    local, mongo = stores(tmp_path)
    res = land("r1", src, files_sink=local, store_sink=mongo)
    assert res.audio_missing
    assert "boom" in res.audio_error
    doc = mongo.collection.find_one({"_id": "r1"})
    assert doc["files"]["audio"] is None
    assert doc["files"]["transcript_md"]
    # a later landing picks the audio up
    del src.get_audio_url
    res2 = land("r1", src, files_sink=local, store_sink=mongo)
    assert not res2.audio_missing
    assert mongo.collection.find_one({"_id": "r1"})["files"]["audio"]["size"] == len(AUDIO)


def test_dry_run_plans_without_writing(tmp_path):
    src = FakePocketSource([make_rec()])
    local, mongo = stores(tmp_path)
    res = land("r1", src, files_sink=local, store_sink=mongo, dry_run=True)
    assert res.dry_run
    assert res.planned
    assert any("files" in p for p in res.planned)
    assert any("store" in p for p in res.planned)
    assert mongo.collection.count_documents({}) == 0
    assert not list((tmp_path / "a").rglob("*.md"))
    assert "get_audio_url" not in src.calls


def test_landing_never_touches_enrichment_beyond_pending(tmp_path):
    src = FakePocketSource([make_rec()])
    local, mongo = stores(tmp_path)
    land("r1", src, files_sink=local, store_sink=mongo)
    assert [d["_id"] for d in mongo.pending_enrichment(10)] == ["r1"]


def test_delete_and_merge_tombstone_only(tmp_path):
    src = FakePocketSource([make_rec("r1"), make_rec("r2")])
    local, mongo = stores(tmp_path)
    land("r1", src, files_sink=local, store_sink=mongo)
    land("r2", src, files_sink=local, store_sink=mongo)
    handle_deleted("r1", files_sink=local, store_sink=mongo, now="2026-10-15T00:00:00Z")
    handle_merged("r2", "r1", files_sink=local, store_sink=mongo, now="2026-10-15T00:00:00Z")
    d1 = mongo.collection.find_one({"_id": "r1"})
    d2 = mongo.collection.find_one({"_id": "r2"})
    assert mongo.get_state("r1").deleted_in_pocket
    assert mongo.get_state("r2").merged_into == "r1"
    assert local.get_state("r1").deleted_in_pocket
    assert local.get_state("r2").merged_into == "r1"
    assert d1["files"]["transcript_md"]
    assert d2["files"]["transcript_md"]
    assert len(list((tmp_path / "a").rglob("transcript.md"))) == 2


def test_unknown_id_swallowed_and_reported(tmp_path):
    local, mongo = stores(tmp_path)
    out = handle_deleted("nope", files_sink=local, store_sink=mongo)
    assert out == {"files": "unknown", "store": "unknown"}
    out = handle_merged("nope", "x", files_sink=local, store_sink=mongo)
    assert out == {"files": "unknown", "store": "unknown"}
    assert mongo.collection.count_documents({}) == 0


def test_rate_limit_on_fetch_propagates(tmp_path):
    src = FakePocketSource([make_rec()])
    src.set_rate_limit(1)
    local, mongo = stores(tmp_path)
    with pytest.raises(CliError):
        land("r1", src, files_sink=local, store_sink=mongo)
    assert mongo.collection.count_documents({}) == 0
