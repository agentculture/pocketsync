"""Real loopback webhook server: auth, limits, dedupe, ack latency."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import queue
import threading
import time

import pytest

from pocketsync.state import MemoryStateStore
from pocketsync.webhook.server import WebhookServer

SECRET = "test-webhook-secret"  # synthetic
PATH = "/hooks/pocket"


def signed_headers(body: bytes, secret: str = SECRET, ts: str | None = None) -> dict[str, str]:
    ts = ts or str(int(time.time()))
    sig = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"X-HeyPocket-Timestamp": ts, "X-HeyPocket-Signature": sig}


def event_body(rid: str = "rec-1", event: str = "recording.completed", ts: str = "t1") -> bytes:
    return json.dumps({"event": event, "timestamp": ts, "recording": {"id": rid}}).encode()


@pytest.fixture
def server():
    state = MemoryStateStore()
    q: queue.Queue = queue.Queue()
    srv = WebhookServer(port=0, secret=SECRET, state=state, queue=q, max_body=1024)
    srv.start()
    yield srv, state, q
    srv.stop()


def request(srv, method, path, body=None, headers=None):
    host, port = srv.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    out = (resp.status, dict(resp.getheaders()), data)
    conn.close()
    return out


def post(srv, body, headers=None):
    h = signed_headers(body) if headers is None else headers
    return request(srv, "POST", PATH, body, h)


def test_binds_loopback_only():
    for host in ("0.0.0.0", "192.168.1.5", ""):
        with pytest.raises(ValueError):
            WebhookServer(
                host=host, port=0, secret=SECRET, state=MemoryStateStore(), queue=queue.Queue()
            )


def test_verified_event_enqueued_and_marked(server):
    srv, state, q = server
    status, headers, _ = post(srv, event_body())
    assert status in (200, 202)
    assert headers["Cache-Control"] == "no-store"
    rid, etype, payload, key = q.get_nowait()
    assert (rid, etype, key) == ("rec-1", "recording.completed", "recording.completed|rec-1|t1")
    assert payload["recording"]["id"] == "rec-1"
    assert state.get_last_webhook_at() is not None
    assert state.mark_seen(key) is False  # already recorded
    assert [e["key"] for e in state.inbox_pending()] == [key]  # durable until handled


def test_duplicate_acked_not_enqueued(server):
    srv, _, q = server
    body = event_body()
    assert post(srv, body)[0] in (200, 202)
    assert post(srv, body)[0] == 200
    assert q.qsize() == 1


def test_bad_signature_401_nothing_stored(server):
    srv, state, q = server
    body = event_body()
    status, _, data = post(srv, body, signed_headers(body, secret="wrong"))
    assert status == 401
    assert b"bad_signature" not in data
    assert q.empty()
    assert state.get_last_webhook_at() is None
    assert state.mark_seen("recording.completed|rec-1|t1") is True
    assert any("bad_signature" in e["message"] for e in state.errors())


def test_missing_headers_401(server):
    srv, _, q = server
    assert post(srv, event_body(), {})[0] == 401
    assert q.empty()


def test_oversize_413_without_reading_body(server):
    srv, _, q = server
    host, port = srv.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.putrequest("POST", PATH)
    conn.putheader("Content-Length", "999999")
    conn.endheaders()  # body never sent
    resp = conn.getresponse()
    assert resp.status == 413
    conn.close()
    assert q.empty()


def test_missing_content_length_411(server):
    srv, _, _ = server
    host, port = srv.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.putrequest("POST", PATH)
    conn.endheaders()
    assert conn.getresponse().status == 411
    conn.close()


def test_malformed_json_after_valid_signature_400(server):
    srv, _, q = server
    assert post(srv, b"not json")[0] == 400
    assert q.empty()


def test_get_root_404_and_other_paths(server):
    srv, _, _ = server
    status, headers, _ = request(srv, "GET", "/")
    assert status == 404
    assert headers["Cache-Control"] == "no-store"
    assert request(srv, "POST", "/other", b"{}")[0] == 404
    assert request(srv, "GET", PATH)[0] == 405


def test_ack_latency_with_slow_consumer():
    state = MemoryStateStore()
    q: queue.Queue = queue.Queue()
    landed: list[str] = []

    def slow_landing() -> None:
        while True:
            item = q.get()
            if item is None:
                return
            time.sleep(3)  # slow fake landing handler
            landed.append(item[0])

    consumer = threading.Thread(target=slow_landing, daemon=True)
    consumer.start()
    srv = WebhookServer(port=0, secret=SECRET, state=state, queue=q)
    srv.start()
    try:
        latencies = []
        for i in range(3):
            body = event_body(rid=f"rec-{i}", ts=f"t{i}")
            t0 = time.monotonic()
            status, _, _ = post(srv, body)
            latencies.append(time.monotonic() - t0)
            assert status in (200, 202)
        assert max(latencies) < 2.0
        assert landed == []  # acked before any landing finished
    finally:
        srv.stop()
        q.put(None)
