"""Audio landing robustness (code review of PR #5, findings 1, 2, 7, 8). All data synthetic."""

from __future__ import annotations

import io
import urllib.error
from datetime import datetime, timezone

import mongomock

from pocketsync.model import Recording, TranscriptSegment
from pocketsync.pocket.fake import FakePocketSource
from pocketsync.reconcile import Reconciler
from pocketsync.sinks.local import LocalSink
from pocketsync.sinks.mongo import MongoSink
from pocketsync.state import MemoryStateStore
from pocketsync.sync import land

NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)


class Resp:
    headers = {"Content-Type": "audio/mpeg"}

    def __init__(self):
        self._io = io.BytesIO(b"synthetic-audio")

    def read(self, n=-1):
        return self._io.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    """Counts downloads; fails for ids in ``fail``."""

    def __init__(self, fail=()):
        self.fail = set(fail)
        self.urls: list[str] = []

    def __call__(self, url):
        self.urls.append(url)
        if any(f"/{rid}?" in url for rid in self.fail):
            raise urllib.error.URLError("synthetic download failure")
        return Resp()


def rec(i, **kw):
    ts = f"2026-10-13T10:00:{i:02d}Z"
    base = dict(
        id=f"r{i:04d}",
        title=f"Synthetic {i}",
        created_at=ts,
        recording_at=ts,
        updated_at=ts,
        segments=[TranscriptSegment(start=0, end=1, text=f"hello {i}")],
        summary="A summary.",
    )
    base.update(kw)
    return Recording(**base)


class Env:
    def __init__(self, tmp_path, recs, opener=None, **kw):
        self.src = FakePocketSource(recs)
        self.opener = opener or Opener()
        self.local = LocalSink(tmp_path / "files", opener=self.opener)
        self.mongo = MongoSink(mongomock.MongoClient()["db"])
        self.state = MemoryStateStore()
        self.kw = kw

    def rc(self, **kw):
        return Reconciler(
            self.src,
            files_sink=self.local,
            store_sink=self.mongo,
            state=self.state,
            sleep=lambda s: None,
            clock=lambda: NOW,
            **{**self.kw, **kw},
        )

    def has_audio(self, rid):
        st = self.local.get_state(rid)
        return bool(st and st.has_audio)


def test_failed_download_still_lands_text_and_the_pass_continues(tmp_path):
    env = Env(tmp_path, [rec(1), rec(2)], opener=Opener(fail={"r0001"}))
    result = env.rc().reconcile()
    assert sorted(d["_id"] for d in env.mongo.collection.find({})) == ["r0001", "r0002"]
    assert env.local.get_state("r0001") is not None  # meta.json written, text landed
    assert not env.has_audio("r0001")
    assert env.has_audio("r0002")
    assert result.landed == 2


def test_land_reports_a_failed_download_as_audio_missing(tmp_path):
    env = Env(tmp_path, [rec(1)], opener=Opener(fail={"r0001"}))
    res = land("r0001", env.src, files_sink=env.local, store_sink=env.mongo)
    assert res.audio_missing
    assert "download" in (res.audio_error or "")


def test_missing_audio_is_retried_by_a_later_pass(tmp_path):
    env = Env(tmp_path, [rec(1)])
    env.rc(with_audio=False).reconcile()
    assert not env.has_audio("r0001")
    env.rc().reconcile()  # same Pocket updated_at, but audio is wanted and missing
    assert env.has_audio("r0001")


def test_no_audio_pass_does_not_reland_unchanged_text_only_recordings(tmp_path):
    env = Env(tmp_path, [rec(1)])
    env.rc(with_audio=False).reconcile()
    second = env.rc(with_audio=False).reconcile()
    assert second.skipped == 1 and second.landed == 0


def test_audio_on_disk_skips_the_audio_url_and_the_redownload(tmp_path):
    env = Env(tmp_path, [rec(1)])
    land("r0001", env.src, files_sink=env.local, store_sink=env.mongo)
    assert len(env.opener.urls) == 1
    env.src.edit("r0001", summary="Pocket re-processed the summary.")
    before = env.src.calls.count("get_audio_url")
    res = land("r0001", env.src, files_sink=env.local, store_sink=env.mongo)
    assert res.files_changed and res.store_changed
    assert env.src.calls.count("get_audio_url") == before  # no new signed URL
    assert len(env.opener.urls) == 1  # audio is immutable; not downloaded again
    assert env.has_audio("r0001")


def test_equivalent_timestamp_formats_are_not_a_change(tmp_path):
    env = Env(tmp_path, [rec(1)])
    env.rc().reconcile()
    env.mongo.collection.update_one(
        {"_id": "r0001"}, {"$set": {"source.updated_at": "2026-10-13T10:00:01+00:00"}}
    )
    second = env.rc().reconcile()
    assert second.skipped == 1 and second.landed == 0


def test_missing_listed_updated_at_is_never_treated_as_unchanged(tmp_path):
    env = Env(tmp_path, [rec(1, updated_at=None)])
    env.rc().reconcile()
    env.src.edit("r0001", summary="Changed while updated_at stays absent.", updated_at=None)
    env.rc().reconcile()
    doc = env.mongo.collection.find_one({"_id": "r0001"})
    assert "Changed while" in doc["summary"]
