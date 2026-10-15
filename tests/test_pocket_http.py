import io
import json
import urllib.error
from unittest import mock

import pytest

from pocketsync.cli._errors import CliError
from pocketsync.pocket.base import PocketSource, RateLimited
from pocketsync.pocket.http import BASE_URL, PocketHttpSource, _map_recording

TARGET = "pocketsync.pocket.http.urllib.request.urlopen"


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok(obj):
    return _Resp(json.dumps(obj).encode())


def _http_error(code, body=b"{}", headers=None):
    return urllib.error.HTTPError("u", code, "x", headers or {}, io.BytesIO(body))


WIRE = {
    "id": "rec1",
    "title": "Standup",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-02T00:00:00Z",
    "recording_at": "2026-01-01T09:00:00Z",
    "duration": 61,
    "language": "en",
    "state": "some-new-state",
    "tags": [{"id": "t", "name": "work"}],
    "speakers": ["Ann"],
    "transcript": [
        {"start": 0, "end": 1.5, "text": "hi", "speaker": "s1", "speakerName": "Ann"},
    ],
    "summarizations": {
        "v2": {
            "summary": {"markdown": "# Sum"},
            "actionItems": {"items": [{"text": "do it", "done": True}, "plain"]},
        }
    },
}


def _src():
    return PocketHttpSource("pk_test")


def test_protocol():
    assert isinstance(_src(), PocketSource)


def test_get_recording_maps_and_sends_auth():
    with mock.patch(TARGET, return_value=_ok({"success": True, "data": WIRE})) as m:
        rec = _src().get_recording("rec1")
    req = m.call_args[0][0]
    assert req.full_url.startswith(BASE_URL + "/recordings/rec1?")
    assert "include_transcript=true" in req.full_url
    assert req.get_header("Authorization") == "Bearer pk_test"
    assert m.call_args[1]["timeout"] > 0
    assert rec.state == "some-new-state"
    assert rec.tags == ["work"]
    assert rec.segments[0].speaker_name == "Ann"
    assert rec.segments[0].end == 1.5
    assert rec.summary == "# Sum"
    assert [(a.text, a.done) for a in rec.action_items] == [("do it", True), ("plain", False)]
    assert rec.raw["id"] == "rec1"


def test_list_recordings_page():
    body = {
        "success": True,
        "data": [WIRE],
        "pagination": {"page": 2, "limit": 1, "total": 5, "total_pages": 5, "has_more": True},
    }
    with mock.patch(TARGET, return_value=_ok(body)) as m:
        page = _src().list_recordings("2026-01-01", "2026-01-31", page=2, limit=1)
    url = m.call_args[0][0].full_url
    assert "start_date=2026-01-01" in url
    assert "page=2" in url
    assert "limit=1" in url
    assert page.has_more
    assert page.total == 5
    assert page.page == 2
    assert page.recordings[0].id == "rec1"


def test_limit_over_100_rejected():
    with pytest.raises(CliError):
        _src().list_recordings(limit=101)


def test_429_with_and_without_retry_after():
    with mock.patch(TARGET, side_effect=_http_error(429, headers={"Retry-After": "12"})):
        with pytest.raises(RateLimited) as ei:
            _src().list_recordings()
    assert ei.value.retry_after == 12.0
    with mock.patch(TARGET, side_effect=_http_error(429)):
        with pytest.raises(RateLimited) as ei:
            _src().list_recordings()
    assert ei.value.retry_after is None


def test_401_is_auth_clierror_without_key_leak():
    body = json.dumps({"success": False, "error": "invalid key"}).encode()
    with mock.patch(TARGET, side_effect=_http_error(401, body)):
        with pytest.raises(CliError) as ei:
            _src().list_recordings()
    assert not isinstance(ei.value, RateLimited)
    assert "API key" in ei.value.message
    assert "pk_test" not in repr(ei.value.to_dict())
    assert "pk_test" not in repr(_src())


def test_network_error_and_5xx():
    with mock.patch(TARGET, side_effect=urllib.error.URLError("boom")):
        with pytest.raises(CliError) as ei:
            _src().list_recordings()
    assert ei.value.code == 2
    with mock.patch(TARGET, side_effect=_http_error(503)):
        with pytest.raises(CliError):
            _src().get_recording("x")


def test_audio_url_string_and_dict():
    for data in ("https://a.example/x", {"url": "https://a.example/x"}):
        with mock.patch(TARGET, return_value=_ok({"success": True, "data": data})) as m:
            assert _src().get_audio_url("rec1", 120) == "https://a.example/x"
        url = m.call_args[0][0].full_url
        assert "/recordings/rec1/audio-url?expires_in=120" in url
    with pytest.raises(CliError):
        _src().get_audio_url("rec1", 10)
    with mock.patch(TARGET, return_value=_ok({"success": True, "data": None})):
        with pytest.raises(CliError):
            _src().get_audio_url("rec1")


def test_map_defensive_alternate_keys():
    rec = _map_recording(
        {
            "id": "a",
            "summarizations": [{"summary": {"markdown": "M"}, "action_items": ["x"]}],
            "transcript": None,
        }
    )
    assert rec.summary == "M"
    assert rec.action_items[0].text == "x"
    assert rec.segments == []
    assert _map_recording({"id": "b"}).summary == ""
