"""Mapping against the shapes Pocket's public API actually returns (verified 2026-10-14).

All values are SYNTHETIC; only the key structure mirrors the live API (observed with a
read-only probe that printed key names/types, never content).
"""

import io
import json
import urllib.error
from unittest import mock

import pytest

from pocketsync.cli._errors import CliError
from pocketsync.pocket.base import RateLimited
from pocketsync.pocket.http import PocketHttpSource, _map_recording

TARGET = "pocketsync.pocket.http.urllib.request.urlopen"


class _Resp(io.BytesIO):
    def __init__(self, body: bytes, headers: dict | None = None):
        super().__init__(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


LIVE_DETAIL = {
    "id": "rec-live-1",
    "title": "Synthetic sync",
    "folder_id": None,
    "duration": 120,
    "state": "completed",
    "language": "en",
    "recording_at": "2026-10-01T09:00:00Z",
    "created_at": "2026-10-01T09:05:00Z",
    "updated_at": "2026-10-01T09:06:00Z",
    "tags": [],
    "transcript": {
        "metadata": {"duration": 120.0, "language": "en", "source": "x"},
        "segments": [
            {
                "start": 0.0,
                "end": 2.5,
                "text": "hello there",
                "originalText": "hello there",
                "speaker": "SPEAKER_00",
                "speakerName": "Alex",
            }
        ],
        "text": "hello there",
    },
    "speakers": {"Alex": {"name": "Alex", "speakerId": "spk-1"}},
    "summarizations": {
        "sum-1": {
            "id": "sum-1",
            "processingStatus": "completed",
            "v2": {
                "summary": {"markdown": "# Synthetic summary", "version": "2"},
                "mindMap": {"nodes": [], "type": "tree"},
                "actionItems": {
                    "actions": [
                        {
                            "id": "ai-1",
                            "label": "Send the synthetic report",
                            "isCompleted": False,
                            "is_completed": False,
                            "assignee": "Alex",
                            "dueDate": None,
                            "priority": "high",
                            "status": "open",
                            "type": "task",
                            "context": "c",
                        },
                        {"id": "ai-2", "label": "Done thing", "isCompleted": True},
                    ],
                    "version": "2",
                },
            },
            "settings": {"modelId": "m"},
        }
    },
}


def test_live_action_items_under_actions_are_mapped():
    rec = _map_recording(LIVE_DETAIL)
    assert [a.text for a in rec.action_items] == ["Send the synthetic report", "Done thing"]
    assert [a.done for a in rec.action_items] == [False, True]
    assert rec.action_items[0].owner == "Alex"


def test_live_summary_segments_and_speakers():
    rec = _map_recording(LIVE_DETAIL)
    assert rec.summary == "# Synthetic summary"
    assert rec.segments[0].speaker_name == "Alex"
    assert rec.segments[0].end == 2.5
    assert rec.speakers == ["Alex"]


def test_audio_url_reads_signed_url():
    body = {
        "success": True,
        "data": {
            "signed_url": "https://audio.example.invalid/x.m4a?sig=1",
            "expires_in": 60,
            "expires_at": "2026-10-01T10:00:00Z",
        },
    }
    with mock.patch(TARGET, return_value=_Resp(json.dumps(body).encode())):
        url = PocketHttpSource("pk_test").get_audio_url("rec-live-1", expires_in=60)
    assert url.endswith("x.m4a?sig=1")


def _page(headers):
    body = {"success": True, "data": [], "pagination": {"page": 1, "has_more": False}}
    return _Resp(json.dumps(body).encode(), headers)


def test_throttles_until_reset_when_remaining_is_zero():
    slept: list[float] = []
    src = PocketHttpSource("pk_test", sleep=slept.append, clock=lambda: 1000.0)
    headers = {"X-Ratelimit-Limit": "50", "X-Ratelimit-Remaining": "0", "X-Ratelimit-Reset": "1030"}
    with mock.patch(TARGET, side_effect=[_page(headers), _page({})]):
        src.list_recordings(limit=1)
        assert slept == []  # the throttle applies before the NEXT request
        src.list_recordings(limit=1)
    assert slept == [pytest.approx(30.0, abs=1.0)]


def test_no_throttle_when_remaining_is_positive():
    slept: list[float] = []
    src = PocketHttpSource("pk_test", sleep=slept.append, clock=lambda: 1000.0)
    headers = {"X-Ratelimit-Remaining": "12", "X-Ratelimit-Reset": "1030"}
    with mock.patch(TARGET, side_effect=[_page(headers), _page({})]):
        src.list_recordings(limit=1)
        src.list_recordings(limit=1)
    assert slept == []


def test_429_without_retry_after_uses_ratelimit_reset():
    src = PocketHttpSource("pk_test", clock=lambda: 1000.0)
    err = urllib.error.HTTPError("u", 429, "x", {"X-Ratelimit-Reset": "1045"}, io.BytesIO(b"{}"))
    with mock.patch(TARGET, side_effect=err):
        with pytest.raises(RateLimited) as ei:
            src.list_recordings(limit=1)
    assert ei.value.retry_after == pytest.approx(45.0, abs=1.0)


def test_429_arms_the_throttle_for_the_next_request():
    # colleague review e008e4e84cb3 (a): a hard 429 must also arm the proactive throttle,
    # otherwise the next call fires immediately and draws another 429.
    slept: list[float] = []
    src = PocketHttpSource("pk_test", sleep=slept.append, clock=lambda: 1000.0)
    err = urllib.error.HTTPError("u", 429, "x", {"X-Ratelimit-Reset": "1040"}, io.BytesIO(b"{}"))
    with mock.patch(TARGET, side_effect=[err, _page({})]):
        with pytest.raises(RateLimited):
            src.list_recordings(limit=1)
        assert slept == []
        src.list_recordings(limit=1)
    assert slept == [pytest.approx(40.0, abs=1.0)]


def test_non_429_error_does_not_arm_the_throttle():
    slept: list[float] = []
    src = PocketHttpSource("pk_test", sleep=slept.append, clock=lambda: 1000.0)
    err = urllib.error.HTTPError("u", 500, "x", {"X-Ratelimit-Reset": "1040"}, io.BytesIO(b"{}"))
    with mock.patch(TARGET, side_effect=[err, _page({})]):
        with pytest.raises(CliError) as ei:
            src.list_recordings(limit=1)
        assert not isinstance(ei.value, RateLimited)
        src.list_recordings(limit=1)
    assert slept == []


def test_armed_throttle_holds_every_caller_until_reset():
    # code review of PR #5 (finding 6): the first waiter must not disarm the window for
    # the other thread that shares this source.
    slept: list[float] = []
    src = PocketHttpSource("pk_test", sleep=slept.append, clock=lambda: 1000.0)
    src._note_throttled({"X-Ratelimit-Reset": "1020"})
    src._await_window()
    src._await_window()
    assert slept == [pytest.approx(20.0, abs=1.0), pytest.approx(20.0, abs=1.0)]


def test_success_with_remaining_does_not_clear_an_armed_throttle():
    slept: list[float] = []
    src = PocketHttpSource("pk_test", sleep=slept.append, clock=lambda: 1000.0)
    src._note_throttled({"X-Ratelimit-Reset": "1020"})
    src._note_rate_limit({"X-Ratelimit-Remaining": "7", "X-Ratelimit-Reset": "1060"})
    src._await_window()
    assert slept == [pytest.approx(20.0, abs=1.0)]


def test_expired_throttle_does_not_wait():
    slept: list[float] = []
    now = [1000.0]
    src = PocketHttpSource("pk_test", sleep=slept.append, clock=lambda: now[0])
    src._note_throttled({"X-Ratelimit-Reset": "1020"})
    now[0] = 1021.0
    src._await_window()
    assert slept == []


def test_audio_url_404_is_audio_not_found():
    from pocketsync.pocket.base import AudioNotFound

    body = io.BytesIO(b'{"success": false, "error": "recording audio file not found"}')
    err = urllib.error.HTTPError("u", 404, "x", {}, body)
    src = PocketHttpSource("pk_test")
    with mock.patch(TARGET, side_effect=err):
        with pytest.raises(AudioNotFound):
            src.get_audio_url("rec-1", expires_in=60)
