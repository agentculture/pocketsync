"""Reconciler/backfill: overlap window, change detection, 429 backoff, webhook-miss counter."""

from __future__ import annotations

import io
import threading
from datetime import datetime, timezone

import mongomock
import pytest

from pocketsync.cli._errors import EXIT_ENV_ERROR, CliError
from pocketsync.model import Recording, TranscriptSegment
from pocketsync.pocket.base import RateLimited
from pocketsync.pocket.fake import FakePocketSource
from pocketsync.reconcile import ReconcilePolicy, Reconciler
from pocketsync.sinks.local import LocalSink
from pocketsync.sinks.mongo import MongoSink
from pocketsync.state import COUNTER_WEBHOOK_MISSED, MemoryStateStore
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


def rec(i, day="2026-10-13", at=None, **kw):
    ts = at or f"{day}T10:{i // 60 % 60:02d}:{i % 60:02d}Z"
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


class CountingSink:
    """Proxy counting put_recording calls and how many actually changed something."""

    def __init__(self, inner):
        self.inner = inner
        self.puts = 0
        self.changes = 0

    def put_recording(self, *a, **kw):
        res = self.inner.put_recording(*a, **kw)
        self.puts += 1
        self.changes += bool(res.changed)
        return res

    def __getattr__(self, name):
        return getattr(self.inner, name)


class Env:
    def __init__(self, tmp_path, recs, **kw):
        self.src = FakePocketSource(recs)
        self.local = CountingSink(LocalSink(tmp_path / "files", opener=lambda u: Resp()))
        self.mongo = CountingSink(MongoSink(mongomock.MongoClient()["db"]))
        self.state = MemoryStateStore()
        self.sleeps: list[float] = []
        self.rc = Reconciler(
            self.src,
            files_sink=self.local,
            store_sink=self.mongo,
            state=self.state,
            sleep=self.sleeps.append,
            clock=lambda: NOW,
            **kw,
        )

    def docs(self):
        return list(self.mongo.inner.collection.find({}))


def test_first_run_lists_last_two_days_and_lands_all(tmp_path):
    env = Env(tmp_path, [rec(1, day="2026-10-12"), rec(2), rec(3, day="2026-10-01")])
    result = env.rc.reconcile()
    assert sorted(d["_id"] for d in env.docs()) == ["r0001", "r0002"]  # r0003 is outside window
    assert result.landed == 2
    assert result.ok
    assert env.state.get_watermark() == "2026-10-14T12:00:00Z"
    assert env.state.last_run()["stats"]["landed"] == 2


def test_window_starts_two_days_before_watermark(tmp_path):
    env = Env(
        tmp_path, [rec(1, day="2026-10-08"), rec(2, day="2026-10-09"), rec(3, day="2026-10-10")]
    )
    env.state.set_watermark("2026-10-11T08:00:00Z")
    env.rc.reconcile()
    assert sorted(d["_id"] for d in env.docs()) == ["r0002", "r0003"]


def test_missed_webhook_counts_only_new_and_old_enough(tmp_path):
    fresh = rec(9, at="2026-10-14T11:55:00Z")  # 5 min old: webhook may still arrive
    env = Env(tmp_path, [rec(1), rec(2), fresh])
    land("r0001", env.src, files_sink=env.local, store_sink=env.mongo)  # webhook delivered r1
    result = env.rc.reconcile()
    assert env.state.counters().get(COUNTER_WEBHOOK_MISSED, 0) == 1  # only r0002
    assert result.missed == 1
    assert result.landed == 2  # r0002 + fresh; r0001 unchanged
    assert {d["_id"] for d in env.docs()} == {"r0001", "r0002", "r0009"}


def test_second_pass_no_changes_writes_nothing(tmp_path):
    env = Env(tmp_path, [rec(i) for i in range(1, 6)])
    env.rc.reconcile()
    pending = env.mongo.inner.pending_enrichment(100)
    assert len(pending) == 5
    before = (env.local.changes, env.mongo.changes, env.local.puts, env.mongo.puts)
    calls = len(env.src.calls)
    again = env.rc.reconcile()
    after = (env.local.changes, env.mongo.changes, env.local.puts, env.mongo.puts)
    assert after == before
    assert again.landed == 0
    assert again.skipped == 5
    assert again.missed == 0
    assert env.state.counters()[COUNTER_WEBHOOK_MISSED] == 5
    assert env.src.calls[calls:] == ["list_recordings"]  # no per-recording fetches
    assert env.mongo.inner.pending_enrichment(100) == pending  # nothing new went pending


def test_changed_updated_at_relands(tmp_path):
    env = Env(tmp_path, [rec(1), rec(2)])
    env.rc.reconcile()
    env.src.edit("r0001", summary="Edited summary.")
    before = env.mongo.changes
    result = env.rc.reconcile()
    assert result.landed == 1
    assert result.missed == 0
    assert env.mongo.changes == before + 1


def test_false_positive_costs_an_api_call_not_a_write(tmp_path):
    env = Env(tmp_path, [rec(1)])
    env.rc.reconcile()
    # Stored timestamp differs from the listing but the content hash is identical.
    env.mongo.inner.collection.update_one({"_id": "r0001"}, {"$set": {"source.updated_at": "x"}})
    before = (env.local.changes, env.mongo.changes)
    result = env.rc.reconcile()
    assert result.landed == 1
    after = (env.local.changes, env.mongo.changes)
    assert after == before


def test_backfill_250_across_three_pages_with_one_429(tmp_path):
    env = Env(tmp_path, [rec(i) for i in range(250)], policy=ReconcilePolicy(page_delay=0))
    env.src.set_rate_limit(1, retry_after=7)
    result = env.rc.backfill("2026-10-01")
    assert len(env.docs()) == 250
    assert result.landed == 250
    assert result.ok
    assert env.sleeps == [7]  # honoured Retry-After exactly once
    assert env.src.calls.count("list_recordings") == 4  # 1 rate-limited + 3 pages
    assert result.pages == 3
    assert env.state.get_watermark() is None  # backfill never moves the watermark
    assert env.state.counters().get(COUNTER_WEBHOOK_MISSED, 0) == 0  # history is not a miss


def test_exponential_backoff_without_retry_after(tmp_path):
    env = Env(tmp_path, [rec(1)], policy=ReconcilePolicy(page_delay=0))
    env.src.set_rate_limit(3)
    env.rc.reconcile()
    assert env.sleeps == [2, 4, 8]
    assert len(env.docs()) == 1


def test_backoff_is_capped(tmp_path):
    env = Env(tmp_path, [rec(1)], policy=ReconcilePolicy(page_delay=0, max_retries=10))
    env.src.set_rate_limit(8)
    env.rc.reconcile()
    assert max(env.sleeps) == 120


def test_rate_limit_during_land_is_retried(tmp_path):
    class Flaky(FakePocketSource):
        tripped = False

        def get_recording(self, *a, **kw):
            if not Flaky.tripped:
                Flaky.tripped = True
                raise RateLimited(3)
            return super().get_recording(*a, **kw)

    env = Env(tmp_path, [rec(1)], policy=ReconcilePolicy(page_delay=0))
    flaky = Flaky([rec(1)])
    env.rc.source = flaky
    result = env.rc.reconcile()
    assert result.ok
    assert len(env.docs()) == 1
    assert env.sleeps == [3]


def test_unrecoverable_rate_limit_keeps_watermark(tmp_path):
    env = Env(tmp_path, [rec(1)], policy=ReconcilePolicy(page_delay=0, max_retries=2))
    env.state.set_watermark("2026-10-13T00:00:00Z")
    env.src.set_rate_limit(99)
    result = env.rc.reconcile()
    assert not result.ok
    assert env.state.get_watermark() == "2026-10-13T00:00:00Z"
    assert env.state.errors()
    assert env.state.errors()[-1]["where"] == "reconcile"


def test_single_recording_failure_keeps_watermark_but_continues(tmp_path):
    class Bad(FakePocketSource):
        def get_recording(self, rid, *a, **kw):
            if rid == "r0001":
                raise CliError(EXIT_ENV_ERROR, "boom", "retry")
            return super().get_recording(rid, *a, **kw)

    env = Env(tmp_path, [rec(1), rec(2)], policy=ReconcilePolicy(page_delay=0))
    env.rc.source = Bad([rec(1), rec(2)])
    result = env.rc.reconcile()
    assert not result.ok
    assert result.landed == 1
    assert result.errors == 1
    assert env.state.get_watermark() is None
    assert [d["_id"] for d in env.docs()] == ["r0002"]


def test_dry_run_writes_nothing(tmp_path):
    env = Env(tmp_path, [rec(1)], dry_run=True)
    result = env.rc.reconcile()
    assert result.planned
    assert env.docs() == []
    assert env.state.get_watermark() is None
    assert env.state.counters().get(COUNTER_WEBHOOK_MISSED, 0) == 0


def test_page_delay_between_pages(tmp_path):
    env = Env(tmp_path, [rec(i) for i in range(150)], policy=ReconcilePolicy(page_delay=0.5))
    env.rc.reconcile()
    assert env.sleeps == [0.5]


def test_run_forever_stops_on_event(tmp_path):
    env = Env(tmp_path, [rec(1)])
    stop = threading.Event()
    waits = []
    stop.wait = lambda timeout=None: waits.append(timeout) or len(waits) >= 2
    env.rc.run_forever(interval=300, stop_event=stop)
    assert waits == [300, 300]
    assert len(env.docs()) == 1


def test_run_forever_survives_errors(tmp_path):
    env = Env(tmp_path, [rec(1)], policy=ReconcilePolicy(page_delay=0, max_retries=0))
    env.src.set_rate_limit(1)
    stop = threading.Event()
    n = []
    stop.wait = lambda timeout=None: n.append(1) or len(n) >= 2
    env.rc.run_forever(interval=1, stop_event=stop)
    assert len(env.docs()) == 1  # second pass recovered


def test_bad_since_is_user_error(tmp_path):
    env = Env(tmp_path, [])
    with pytest.raises(CliError):
        env.rc.backfill("not-a-date")


# -- automatic catch-up (d6): fresh install, downtime, upgrade -----------------------------


def test_fresh_install_backfills_from_the_initial_date(tmp_path):
    from pocketsync.reconcile import LANDING_SCHEMA

    env = Env(tmp_path, [rec(1, day="2025-03-01"), rec(2)], initial_since="2025-01-01")
    result = env.rc.reconcile()
    assert sorted(d["_id"] for d in env.docs()) == ["r0001", "r0002"]
    assert result.ok
    assert result.catch_up == "initial"
    assert result.missed == 0  # a backfill is not a missed webhook
    assert env.state.get_watermark() == "2026-10-14T12:00:00Z"
    assert env.state.get_landing_schema() == LANDING_SCHEMA
    second = env.rc.reconcile()  # now incremental
    assert second.catch_up is None
    assert second.landed == 0


def test_downtime_longer_than_the_overlap_is_caught_up(tmp_path):
    env = Env(tmp_path, [rec(1, day="2026-09-20")], initial_since="2025-01-01")
    env.rc.reconcile()  # baseline: fresh-install backfill
    env.src.seed([rec(2, day="2026-09-25")])  # landed in Pocket while we were down
    env.state.set_watermark("2026-09-24T00:00:00Z")  # last good pass, 20 days ago
    result = env.rc.reconcile()
    assert "r0002" in {d["_id"] for d in env.docs()}
    assert result.catch_up is None
    assert result.ok


def test_upgrade_resyncs_every_recording_once(tmp_path):
    env = Env(tmp_path, [rec(1, day="2025-06-01"), rec(2)], initial_since="2025-01-01")
    env.rc.reconcile()
    puts_before = env.mongo.puts
    env.state.set_landing_schema(0)  # written by an older pocketsync
    result = env.rc.reconcile()
    assert result.catch_up == "upgrade"
    assert result.landed == 2
    assert result.skipped == 0
    assert env.mongo.puts == puts_before + 2
    assert env.rc.reconcile().landed == 0  # schema recorded; back to incremental


def test_failed_catch_up_is_retried_by_the_next_pass(tmp_path):
    env = Env(tmp_path, [rec(1, day="2025-06-01")], initial_since="2025-01-01")
    env.src.set_rate_limit(100)  # every call is throttled past the retry budget
    result = env.rc.reconcile()
    assert not result.ok
    assert env.state.get_watermark() is None
    assert env.state.get_landing_schema() is None
    env.src.set_rate_limit(0)
    assert env.rc.reconcile().catch_up == "initial"
