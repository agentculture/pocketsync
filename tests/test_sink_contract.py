"""Backend-neutral Sink contract. t8 adds a "mongo" entry to ``SINK_FACTORIES`` (or overrides
the ``sink`` fixture in its own conftest); the tests below must pass for every backend."""

from __future__ import annotations

import pytest

from pocketsync.model import Recording, TranscriptSegment
from pocketsync.sinks.base import Sink
from pocketsync.sinks.local import LocalSink

SINK_FACTORIES = {"local": lambda tmp_path: LocalSink(tmp_path / "files")}


@pytest.fixture(params=sorted(SINK_FACTORIES))
def sink(request, tmp_path):
    return SINK_FACTORIES[request.param](tmp_path)


def rec(rid="c1", summary="s"):
    return Recording(
        id=rid,
        title="T",
        recording_at="2026-05-01T00:00:00Z",
        summary=summary,
        segments=[TranscriptSegment(start=0, end=1, text="hi")],
    )


def test_is_sink(sink):
    assert isinstance(sink, Sink)


def test_unknown_state_is_none(sink):
    assert sink.get_state("missing") is None


def test_put_then_state(sink):
    r = rec()
    res = sink.put_recording(r)
    assert res.changed and res.content_hash == r.content_hash()
    st = sink.get_state("c1")
    assert st.content_hash == res.content_hash
    assert not st.deleted_in_pocket and st.merged_into is None


def test_idempotent_and_update(sink):
    assert sink.put_recording(rec()).changed
    assert not sink.put_recording(rec()).changed
    res = sink.put_recording(rec(summary="different"))
    assert res.changed
    assert sink.get_state("c1").content_hash == res.content_hash


def test_dry_run_persists_nothing(sink):
    assert sink.put_recording(rec(), dry_run=True).changed
    assert sink.get_state("c1") is None


def test_tombstone_and_merge(sink):
    sink.put_recording(rec("c1"))
    sink.put_recording(rec("c2"))
    sink.tombstone("c1", "2026-06-01T00:00:00Z")
    assert sink.get_state("c1").deleted_in_pocket
    sink.mark_merged("c2", "c1", "2026-06-02T00:00:00Z")
    assert sink.get_state("c2").merged_into == "c1"
    assert sink.get_state("c2").content_hash == rec("c2").content_hash()
