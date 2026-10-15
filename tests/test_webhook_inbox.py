"""Durable webhook inbox (code review of PR #5, finding 4). Synthetic data only.

A webhook is acknowledged once its dedupe key is persisted, so the key must carry the event
until it is handled: a crash, restart or failed handler leaves it ``pending`` and it is
replayed, instead of Pocket's redelivery being dropped as a duplicate.
"""

from __future__ import annotations

import mongomock
import pytest

from pocketsync.serve import Service, ServiceTiming
from pocketsync.state import INBOX_MAX_ATTEMPTS, MemoryStateStore, MongoStateStore
from tests.cli_fakes import SECRET, FakeRuntime, install, rec

ITEM = {"recording_id": "rec-1", "event_type": "recording.deleted", "payload": {}}


@pytest.fixture(params=["memory", "mongo"])
def state(request):
    if request.param == "memory":
        return MemoryStateStore()
    return MongoStateStore(mongomock.MongoClient()["db"])


def test_marked_event_stays_pending_until_done(state):
    assert state.mark_seen("k1", item=ITEM) is True
    assert state.mark_seen("k1", item=ITEM) is False  # still a duplicate for Pocket
    assert [e["key"] for e in state.inbox_pending()] == ["k1"]
    assert state.inbox_pending()[0]["item"] == ITEM
    state.inbox_done("k1")
    assert state.inbox_pending() == []
    assert state.seen("k1")


def test_plain_mark_seen_is_not_an_inbox_entry(state):
    state.mark_seen("k2")
    assert state.inbox_pending() == []


def test_failed_attempts_give_up_after_the_cap(state):
    state.mark_seen("k3", item=ITEM)
    for _ in range(INBOX_MAX_ATTEMPTS - 1):
        state.inbox_fail("k3", "synthetic failure")
        assert [e["key"] for e in state.inbox_pending()] == ["k3"]
    state.inbox_fail("k3", "synthetic failure")
    assert state.inbox_pending() == []


class FlakyStore:
    """Store sink whose tombstone fails ``fail`` times, then delegates."""

    def __init__(self, inner, fail=1):
        self.inner, self.fail = inner, fail

    def tombstone(self, *a, **kw):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("synthetic mongo blip")
        return self.inner.tombstone(*a, **kw)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _service(rt, store_sink, state):
    return Service(
        rt.pocket,
        files_sink=rt.files,
        store_sink=store_sink,
        state=state,
        secret=SECRET,
        port=0,
        timing=ServiceTiming(join_timeout=5.0),
    )


def _deleted(rt, rid):
    return rt.db["recordings"].find_one({"_id": rid})["deleted_in_pocket"]


def test_failed_handling_is_replayed_by_the_next_start(tmp_path, monkeypatch):
    rt = install(monkeypatch, FakeRuntime(tmp_path, [rec(1)]))
    rid = rec(1).id
    rt.reconciler().reconcile()
    state = MongoStateStore(rt.db)
    item = {"recording_id": rid, "event_type": "recording.deleted", "payload": {}}
    assert state.mark_seen("evt-del", item=item)

    first = _service(rt, FlakyStore(rt.store, fail=1), state)
    assert first.process_pending() == 0  # the handler failed; the event stays pending
    assert [e["key"] for e in state.inbox_pending()] == ["evt-del"]
    assert not _deleted(rt, rid)

    second = _service(rt, rt.store, state)  # e.g. after a restart
    assert second.process_pending() == 1
    assert state.inbox_pending() == []
    assert _deleted(rt, rid)


def test_handle_reports_success(tmp_path, monkeypatch):
    rt = install(monkeypatch, FakeRuntime(tmp_path, [rec(1)]))
    svc = _service(rt, rt.store, MongoStateStore(rt.db))
    assert svc.handle(rec(1).id, "recording.completed", {}) is True
    assert svc.handle(None, "recording.completed", {}) is False


def test_live_webhook_entry_is_settled_after_handling(tmp_path, monkeypatch):
    from tests.test_cli_serve import post_event, wait_for

    rt = install(monkeypatch, FakeRuntime(tmp_path, [rec(1)]))
    state = MongoStateStore(rt.db)
    svc = _service(rt, rt.store, state)
    svc.start()
    try:
        assert post_event(svc.port, "recording.completed", rec(1).id) == 202
        assert wait_for(lambda: svc.stats["landed"] == 1)
        assert wait_for(lambda: state.inbox_pending() == [])
    finally:
        svc.stop()


def test_start_requeues_docs_classified_under_a_stale_taxonomy(tmp_path, monkeypatch):
    rt = install(monkeypatch, FakeRuntime(tmp_path, [rec(1)]))

    class Worker:
        calls = 0

        def reclassify_stale(self):
            Worker.calls += 1
            return 0

        def run_once(self):
            return {}

    svc = Service(
        rt.pocket,
        files_sink=rt.files,
        store_sink=rt.store,
        state=MongoStateStore(rt.db),
        secret=SECRET,
        port=0,
        worker=Worker(),
        timing=ServiceTiming(join_timeout=5.0),
    )
    svc.start()
    svc.stop()
    assert Worker.calls == 1
