"""Offline tests for pocketsync.model (synthetic data only)."""

from __future__ import annotations

import copy

from pocketsync.model import ActionItem, Recording, TranscriptSegment, content_hash


def _rec(**kw) -> Recording:
    base = dict(
        id="rec-1",
        title="Standup",
        description="d",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:05:00Z",
        recording_at="2026-01-01T00:00:00Z",
        duration=12.5,
        language="en",
        state="completed",
        folder_id="f1",
        tags=["a", "b"],
        speakers=["Alice", "Bob"],
        segments=[
            TranscriptSegment(start=0.0, end=1.5, text="hi", speaker="s0", speaker_name="Alice")
        ],
        summary="# Sum",
        action_items=[ActionItem(text="do it", done=False)],
        raw={"x": 1, "nested": {"b": 2, "a": 1}},
        fetched_at="2026-02-01T00:00:00Z",
    )
    base.update(kw)
    return Recording(**base)


def test_round_trip():
    r = _rec()
    assert Recording.from_dict(r.to_dict()) == r


def test_from_dict_tolerates_missing_optionals():
    r = Recording.from_dict({"id": "x"})
    assert r.id == "x" and r.segments == [] and r.tags == [] and r.raw == {}


def test_hash_stable_across_key_order():
    a = _rec(raw={"x": 1, "nested": {"b": 2, "a": 1}})
    b = _rec(raw={"nested": {"a": 1, "b": 2}, "x": 1})
    assert content_hash(a) == content_hash(b)
    assert a.content_hash() == content_hash(a)


def test_hash_excludes_fetched_at_including_raw():
    a = _rec(fetched_at="2026-02-01T00:00:00Z", raw={"x": 1, "fetched_at": "t1"})
    b = _rec(fetched_at="2027-02-01T00:00:00Z", raw={"x": 1, "fetched_at": "t2"})
    assert content_hash(a) == content_hash(b)


def test_hash_changes_with_content():
    base = content_hash(_rec())
    assert content_hash(_rec(summary="other")) != base
    r = _rec()
    segs = copy.deepcopy(r.segments)
    segs[0].text = "changed"
    assert content_hash(_rec(segments=segs)) != base
    assert content_hash(_rec(raw={"x": 2})) != base


def test_hash_is_sha256_hex():
    h = content_hash(_rec())
    assert len(h) == 64 and int(h, 16) >= 0
