"""Offline tests for the local sink and the streaming audio downloader."""

from __future__ import annotations

import hashlib
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from pocketsync.cli._errors import CliError
from pocketsync.model import ActionItem, Recording, TranscriptSegment
from pocketsync.sinks.local import LocalSink, download_audio

PAYLOAD = b"synthetic-audio-bytes" * 5000


def make_rec(rid="rec1", **kw):
    base = dict(
        id=rid,
        title="Synthetic",
        recording_at="2026-03-05T10:00:00Z",
        segments=[TranscriptSegment(start=1, end=2, text="hello", speaker_name="A")],
        summary="A summary.",
        action_items=[ActionItem(text="do it", owner="bob")],
    )
    base.update(kw)
    return Recording(**base)


class FakeResp:
    def __init__(self, data, ctype=None, fail_after=None):
        self._io = io.BytesIO(data)
        self.headers = {"Content-Type": ctype} if ctype else {}
        self.fail_after = fail_after
        self.sent = 0

    def read(self, n=-1):
        if self.fail_after is not None and self.sent >= self.fail_after:
            raise ConnectionResetError("simulated mid-stream error")
        b = self._io.read(n)
        self.sent += len(b)
        return b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_download_hashes_and_sizes(tmp_path):
    ref = download_audio(
        "https://x.example/a.mp3", tmp_path, opener=lambda u: FakeResp(PAYLOAD), chunk=1000
    )
    assert ref.rel_path == "audio.mp3"
    assert ref.size == len(PAYLOAD)
    assert ref.sha256 == hashlib.sha256(PAYLOAD).hexdigest()
    assert (tmp_path / "audio.mp3").read_bytes() == PAYLOAD
    assert [p.name for p in tmp_path.iterdir()] == ["audio.mp3"]


def test_extension_from_content_type_then_default(tmp_path):
    r = download_audio(
        "https://x.example/blob", tmp_path, opener=lambda u: FakeResp(b"x", "audio/mpeg")
    )
    assert r.rel_path == "audio.mp3"
    r = download_audio("https://x.example/blob", tmp_path, opener=lambda u: FakeResp(b"x"))
    assert r.rel_path == "audio.m4a"


def test_partial_download_leaves_no_final_file(tmp_path):
    with pytest.raises(ConnectionResetError):
        download_audio(
            "https://x.example/a.m4a",
            tmp_path,
            opener=lambda u: FakeResp(PAYLOAD, fail_after=2000),
            chunk=1000,
        )
    assert list(tmp_path.iterdir()) == []


def test_partial_download_keeps_previous_audio(tmp_path):
    (tmp_path / "audio.m4a").write_bytes(b"old")
    with pytest.raises(ConnectionResetError):
        download_audio(
            "https://x.example/a.m4a", tmp_path, opener=lambda u: FakeResp(PAYLOAD, fail_after=10)
        )
    assert (tmp_path / "audio.m4a").read_bytes() == b"old"


@pytest.mark.parametrize("url", ["http://evil.example/a", "file:///etc/passwd", "ftp://x/a"])
def test_rejects_unsafe_urls(tmp_path, url):
    with pytest.raises(CliError):
        download_audio(url, tmp_path, opener=lambda u: FakeResp(b"x"))


def test_real_http_localhost_stream(tmp_path):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        ref = download_audio(f"http://127.0.0.1:{srv.server_port}/x?expires_in=5", tmp_path)
    finally:
        srv.shutdown()
        srv.server_close()
    assert ref.rel_path == "audio.wav"
    assert ref.sha256 == hashlib.sha256(PAYLOAD).hexdigest()


def test_layout_and_meta_last(tmp_path, monkeypatch):
    order = []
    import pocketsync.sinks.local as mod

    real = mod._atomic_write

    def spy(path, data):
        order.append(path.name)
        real(path, data)

    monkeypatch.setattr(mod, "_atomic_write", spy)
    sink = LocalSink(tmp_path, opener=lambda u: FakeResp(PAYLOAD, "audio/mp4"))
    res = sink.put_recording(make_rec(), audio_url="https://x.example/a")
    d = tmp_path / "2026" / "03" / "rec1"
    assert {p.name for p in d.iterdir()} == {
        "transcript.json",
        "transcript.md",
        "summary.md",
        "audio.m4a",
        "meta.json",
    }
    assert order[-1] == "meta.json"
    meta = json.loads((d / "meta.json").read_text())
    assert meta["content_hash"] == res.content_hash
    assert meta["files"]["audio"]["rel_path"] == "2026/03/rec1/audio.m4a"
    assert meta["files"]["audio"]["sha256"] == hashlib.sha256(PAYLOAD).hexdigest()
    assert res.files.audio.size == len(PAYLOAD)
    assert res.files.meta.rel_path == "2026/03/rec1/meta.json"
    assert "raw" not in meta
    assert "hello" in (d / "transcript.md").read_text()
    assert "do it" in (d / "summary.md").read_text()


def test_failed_audio_writes_no_meta(tmp_path):
    sink = LocalSink(tmp_path, opener=lambda u: FakeResp(PAYLOAD, fail_after=100))
    with pytest.raises(ConnectionResetError):
        sink.put_recording(make_rec(), audio_url="https://x.example/a")
    d = tmp_path / "2026" / "03" / "rec1"
    assert not (d / "meta.json").exists()
    assert not list(d.glob("audio*"))
    assert sink.get_state("rec1") is None


def test_unchanged_is_noop_and_changed_rewrites(tmp_path):
    sink = LocalSink(tmp_path)
    r1 = sink.put_recording(make_rec())
    meta = tmp_path / "2026/03/rec1/meta.json"
    mtime = meta.stat().st_mtime_ns
    r2 = sink.put_recording(make_rec(fetched_at="2030-01-01T00:00:00Z"))
    assert r1.changed and not r2.changed
    assert meta.stat().st_mtime_ns == mtime
    r3 = sink.put_recording(make_rec(summary="new"))
    assert r3.changed and r3.content_hash != r1.content_hash
    assert "new" in (tmp_path / "2026/03/rec1/summary.md").read_text()


def test_dry_run_writes_nothing(tmp_path):
    res = LocalSink(tmp_path / "root").put_recording(make_rec(), dry_run=True)
    assert res.changed
    assert not (tmp_path / "root").exists()


def test_date_fallbacks(tmp_path):
    sink = LocalSink(tmp_path)
    sink.put_recording(make_rec("a", recording_at=None, created_at="2025-12-31T23:00:00Z"))
    sink.put_recording(make_rec("b", recording_at=None, fetched_at="2024-01-02T00:00:00Z"))
    assert (tmp_path / "2025/12/a/meta.json").exists()
    assert (tmp_path / "2024/01/b/meta.json").exists()


def test_tombstone_and_merge_keep_files(tmp_path):
    sink = LocalSink(tmp_path)
    sink.put_recording(make_rec("a"))
    sink.tombstone("a", "2026-04-01T00:00:00Z")
    sink.mark_merged("a", "b", "2026-04-02T00:00:00Z")
    st = sink.get_state("a")
    assert st.deleted_in_pocket and st.merged_into == "b"
    assert (tmp_path / "2026/03/a/transcript.json").exists()
    # a later re-put keeps the flags
    sink.put_recording(make_rec("a", summary="changed"))
    assert sink.get_state("a").deleted_in_pocket
    with pytest.raises(CliError):
        sink.tombstone("nope", "t")


def test_unsafe_id_rejected(tmp_path):
    with pytest.raises(CliError):
        LocalSink(tmp_path).put_recording(make_rec("../evil"))
