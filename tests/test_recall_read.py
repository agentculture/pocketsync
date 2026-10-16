"""Unit tests for pocketsync.recall.read (synthetic data on mongomock)."""

from __future__ import annotations

import json

import mongomock
import pytest

from pocketsync import vindex
from pocketsync.cli._errors import CliError
from pocketsync.recall.read import (
    get_recording,
    get_transcript,
    list_action_items,
    list_recordings,
)
from pocketsync.sinks.mongo import MongoSink


def _rec(rid, at, labels=("meeting",), sensitive=None, **kw):
    filters = {} if sensitive is None else {"sensitive": sensitive}
    doc = {
        "_id": rid,
        "title": f"Synthetic {rid}",
        "recording_at": at,
        "duration": 60.0,
        "summary": f"summary of {rid}",
        "action_items": [
            {"text": f"{rid} open task", "done": False, "owner": "me"},
            {"text": f"{rid} done task", "done": True, "owner": None},
        ],
        "segments": [
            {"start": 0.0, "end": 5.0, "text": "hello", "speaker": "S1", "speaker_name": "Ann"},
            {"start": 5.0, "end": 12.0, "text": "middle", "speaker": "S2", "speaker_name": None},
            {"start": 12.0, "end": 20.0, "text": "bye", "speaker": "S1", "speaker_name": "Ann"},
        ],
        "deleted_in_pocket": False,
        "merged_into": None,
        "files": {"meta": "2026/10/x/meta.json"},
        "enrichment": {
            "status": "done",
            "labels": list(labels),
            "filters": filters,
            "summary_vector": [0.1, 0.2],
            "chunks": [{"index": 0, "start": 0.0, "end": 20.0, "text": "t", "vector": [0.3]}],
        },
    }
    doc.update(kw)
    return doc


@pytest.fixture
def col():
    c = mongomock.MongoClient()["pocketsync"][vindex.COLLECTION]
    c.insert_many(
        [
            _rec("a", "2026-10-01T09:00:00Z"),
            _rec("b", "2026-10-05T09:00:00Z", labels=("personal",), sensitive=0.9),
            _rec("c", "2026-10-09T09:00:00Z", sensitive=0.1),
            _rec("del", "2026-10-06T09:00:00Z", deleted_in_pocket=True),
            _rec("mrg", "2026-10-07T09:00:00Z", merged_into="a"),
        ]
    )
    return c


def _no_vectors(out) -> None:
    blob = json.dumps(out)
    assert "vector" not in blob


def test_get_recording_returns_fields_without_vectors(col) -> None:
    out = get_recording(col, "b")
    assert out["id"] == "b"
    assert out["title"] == "Synthetic b"
    assert out["recording_at"] == "2026-10-05T09:00:00Z"
    assert out["summary"] == "summary of b"
    assert out["labels"] == ["personal"]
    assert out["filters"] == {"sensitive": 0.9}
    assert out["action_items"][0] == {"text": "b open task", "done": False, "owner": "me"}
    assert out["segment_count"] == 3
    assert out["enrichment_status"] == "done"
    assert "segments" not in out
    _no_vectors(out)


def test_get_recording_unknown_id_is_a_user_error(col) -> None:
    with pytest.raises(CliError) as exc:
        get_recording(col, "nope")
    assert exc.value.code == 1


def test_get_recording_accepts_a_mongo_sink() -> None:
    sink = MongoSink(mongomock.MongoClient()["pocketsync"])
    sink.collection.insert_one(_rec("s", "2026-10-01T00:00:00Z"))
    assert get_recording(sink, "s")["id"] == "s"


def test_get_transcript_full_and_windowed(col) -> None:
    full = get_transcript(col, "a")
    assert [s["text"] for s in full["segments"]] == ["hello", "middle", "bye"]
    assert full["segments"][0] == {
        "start": 0.0,
        "end": 5.0,
        "text": "hello",
        "speaker": "S1",
        "speaker_name": "Ann",
    }
    win = get_transcript(col, "a", start=6.0, end=13.0)
    assert [s["text"] for s in win["segments"]] == ["middle", "bye"]  # overlapping segments
    assert (win["start"], win["end"]) == (6.0, 13.0)
    assert [s["text"] for s in get_transcript(col, "a", start=15.0)["segments"]] == ["bye"]
    assert [s["text"] for s in get_transcript(col, "a", end=4.0)["segments"]] == ["hello"]
    _no_vectors(full)


def test_get_transcript_rejects_bad_window_and_unknown_id(col) -> None:
    with pytest.raises(CliError):
        get_transcript(col, "a", start=10.0, end=2.0)
    with pytest.raises(CliError):
        get_transcript(col, "nope")


def test_list_recordings_newest_first_excluding_deleted_and_merged(col) -> None:
    out = list_recordings(col)
    assert [r["id"] for r in out["recordings"]] == ["c", "b", "a"]
    assert out["count"] == 3
    first = out["recordings"][0]
    assert first["title"] == "Synthetic c"
    assert first["labels"] == ["meeting"]
    assert first["action_items"] == 2
    _no_vectors(out)


def test_list_recordings_filters(col) -> None:
    ids = lambda out: [r["id"] for r in out["recordings"]]  # noqa: E731
    assert ids(list_recordings(col, label="personal")) == ["b"]
    assert ids(list_recordings(col, since="2026-10-05")) == ["c", "b"]
    assert ids(list_recordings(col, until="2026-10-05")) == ["b", "a"]
    assert ids(list_recordings(col, limit=1)) == ["c"]
    assert ids(list_recordings(col, exclude_sensitive=True)) == ["c", "a"]
    with pytest.raises(CliError):
        list_recordings(col, limit=-1)


def test_list_action_items(col) -> None:
    out = list_action_items(col)
    assert out["count"] == 6
    texts = [a["text"] for a in out["action_items"]]
    assert "del open task" not in texts and "mrg open task" not in texts
    first = out["action_items"][0]
    assert first == {
        "recording_id": "c",
        "title": "Synthetic c",
        "recording_at": "2026-10-09T09:00:00Z",
        "index": 0,
        "text": "c open task",
        "done": False,
        "owner": "me",
    }
    opened = list_action_items(col, open_only=True)
    assert [a["text"] for a in opened["action_items"]] == [
        "c open task",
        "b open task",
        "a open task",
    ]
    sens = list_action_items(col, open_only=True, exclude_sensitive=True)
    assert [a["recording_id"] for a in sens["action_items"]] == ["c", "a"]
    _no_vectors(out)
