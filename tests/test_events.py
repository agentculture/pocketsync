"""Offline tests for pocketsync.events (outbox + QoS 1 publisher)."""

import json
import subprocess
import sys
from types import SimpleNamespace

from pocketsync.events import EVENT_TYPE, Outbox, OutboxPublisher


def _emit(box, rid="r1", h="h1", **kw):
    return box.append(
        rid,
        h,
        mongo={"collection": "recordings", "doc_id": rid},
        files={"meta": f"2026/10/{rid}/meta.json"},
        labels=["meeting"],
        taxonomy_version="1",
        model="m",
        **kw,
    )


class FakeClient:
    def __init__(self, fail_after=None, raises=False):
        self.calls, self.fail_after, self.raises = [], fail_after, raises

    def publish_event(self, envelope, topic, *, qos=1, retain=False, wait=0.0):
        if self.raises:
            raise ConnectionRefusedError("broker down")
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            return SimpleNamespace(ok=False, reason="no_conn")
        self.calls.append((envelope, topic, qos))
        return SimpleNamespace(ok=True, reason="")


def _factory(event):
    return {"id": event["event_id"], "data": event}, "events/pocketsync/x"


def test_append_schema_and_dedupe(tmp_path):
    box = Outbox(tmp_path)
    ev = _emit(box)
    assert ev["type"] == EVENT_TYPE and ev["change"] == "created"
    assert ev["sinks"]["mongo"] == {"collection": "recordings", "doc_id": "r1"}
    assert ev["enrichment"]["labels"] == ["meeting"]
    assert _emit(box) is None
    assert _emit(Outbox(tmp_path)) is None  # index reloaded from file
    assert len((tmp_path / "events.jsonl").read_text().splitlines()) == 1


def test_updated_on_new_hash(tmp_path):
    box = Outbox(tmp_path)
    _emit(box)
    assert _emit(box, h="h2")["change"] == "updated"
    assert _emit(Outbox(tmp_path), rid="r1", h="h3")["change"] == "updated"


def test_torn_tail_ignored_and_healed(tmp_path):
    box = Outbox(tmp_path)
    _emit(box)
    with open(tmp_path / "events.jsonl", "ab") as fh:
        fh.write(b'{"event_id": "tor')
    _emit(Outbox(tmp_path), rid="r2")
    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    assert json.loads(lines[-1])["recording_id"] == "r2"


def test_drain_publishes_qos1_and_is_idempotent(tmp_path):
    box = Outbox(tmp_path)
    _emit(box)
    _emit(box, rid="r2")
    client = FakeClient()
    pub = OutboxPublisher(tmp_path, client, _factory)
    s = pub.drain()
    assert (s.published, s.pending) == (2, 0)
    assert [q for _, _, q in client.calls] == [1, 1]
    assert pub.drain().published == 0 and len(client.calls) == 2


def test_broker_down_leaves_pending_then_publishes(tmp_path):
    box = Outbox(tmp_path)
    _emit(box)
    _emit(box, rid="r2")
    down = OutboxPublisher(tmp_path, FakeClient(raises=True), _factory).drain()
    assert (down.published, down.pending) == (0, 2) and down.error
    partial = OutboxPublisher(tmp_path, FakeClient(fail_after=1), _factory).drain()
    assert (partial.published, partial.pending) == (1, 1)
    client = FakeClient()
    s = OutboxPublisher(tmp_path, client, _factory).drain()
    assert (s.published, s.pending) == (1, 0)
    assert client.calls[0][0]["data"]["recording_id"] == "r2"


def test_import_without_events_cli():
    code = (
        "import sys; sys.modules['events_cli']=None\n"
        "import pocketsync.events as e\n"
        "s=e.OutboxPublisher('.').drain(); assert s.published==0\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd="/tmp")
    assert r.returncode == 0, r.stderr
