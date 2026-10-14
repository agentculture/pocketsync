"""`pocketsync serve` / `pocketsync watch` and the :class:`pocketsync.serve.Service` composition."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import signal
import subprocess  # nosec B404 - test drives a child interpreter
import sys
import threading
import time
from pathlib import Path

import pytest

from pocketsync import vindex
from pocketsync.cli import main
from pocketsync.cli._commands import serve as serve_cmd
from pocketsync.cli._commands import watch as watch_cmd
from pocketsync.serve import Service, merge_ids
from tests.cli_fakes import SECRET, FakeRuntime, install, rec

REPO = Path(__file__).resolve().parents[1]


def wait_for(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def post_event(port, event, rid, ts="t1", **extra):
    body = json.dumps({"event": event, "timestamp": ts, "recording": {"id": rid}, **extra}).encode()
    now = str(int(time.time()))
    sig = hmac.new(SECRET.encode(), now.encode() + b"." + body, hashlib.sha256).hexdigest()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(
        "POST",
        "/hooks/pocket",
        body=body,
        headers={"X-HeyPocket-Timestamp": now, "X-HeyPocket-Signature": sig},
    )
    status = conn.getresponse().status
    conn.close()
    return status


def make_service(rt, **kw):
    opts = dict(
        source=rt.pocket,
        files_sink=rt.files,
        store_sink=rt.store,
        state=rt.counting_state,
        secret=SECRET,
        port=0,
        reconciler=rt.reconciler(),
        worker=rt.worker(),
        publisher=rt.publisher(),
        reconcile_interval=3600,
        enrich_interval=0.05,
        drain_interval=0.05,
        join_timeout=5.0,
    )
    opts.update(kw)
    return Service(**opts)


@pytest.fixture
def rt(tmp_path, monkeypatch):
    return install(monkeypatch, FakeRuntime(tmp_path, [rec(1), rec(2)]))


def doc(rt, rid):
    return rt.db["recordings"].find_one({"_id": rid})


def test_service_runs_all_four_loops_and_stops_cleanly(rt):
    svc = make_service(rt)
    svc.start()
    try:
        # reconciler pass at start lands the seeded recordings without any webhook
        assert wait_for(lambda: doc(rt, "r0001") and doc(rt, "r0002"))
        # receiver + landing consumer: a webhook for a new recording lands it
        rt.pocket.seed([rec(3)])
        assert post_event(svc.port, "transcription.completed", "r0003") == 202
        assert wait_for(lambda: doc(rt, "r0003") is not None)
        # enrichment worker + outbox drain
        assert wait_for(
            lambda: rt.db["recordings"].count_documents({"enrichment.status": "done"}) == 3
        )
        assert wait_for(lambda: len(rt.events_client.published) == 3)
        assert set(svc.thread_names()) == {"landing", "reconciler", "enrichment", "outbox"}
    finally:
        leftover = svc.stop()
    assert leftover == []
    assert not any(t.is_alive() for t in svc.threads)
    assert svc.stop_event.is_set()


def test_deleted_and_merged_events_route_to_handlers(rt):
    svc = make_service(rt, reconciler=None, worker=None, publisher=None)
    svc.handle("r0001", "recording.created", {})
    svc.handle("r0002", "recording.created", {})
    svc.handle("r0001", "recording.deleted", {})
    assert doc(rt, "r0001")["deleted_in_pocket"] is True
    rt.pocket.seed([rec(5)])
    svc.handle("r0005", "recording.created", {})
    rt.pocket.merge("r0005", "r0002")
    svc.handle("r0002", "recording.merged", {"merged_recording_ids": ["r0005"]})
    assert doc(rt, "r0005")["merged_into"] == "r0002"
    assert svc.stats["merged"] == 1 and svc.stats["deleted"] == 1


def test_merged_without_ids_records_error(rt):
    svc = make_service(rt, reconciler=None, worker=None, publisher=None)
    svc.handle("r0001", "recording.merged", {})
    assert any("merge ids" in e["message"] for e in rt.counting_state.errors())


def test_event_without_recording_id_is_recorded_not_raised(rt):
    svc = make_service(rt, reconciler=None, worker=None, publisher=None)
    svc.handle(None, "transcription.completed", {})
    assert rt.counting_state.errors()[-1]["where"] == "serve"


def test_landing_errors_are_recorded_and_consumer_survives(rt):
    svc = make_service(rt, reconciler=None, worker=None, publisher=None)
    svc.start()
    try:
        assert post_event(svc.port, "transcription.completed", "nope") == 202
        assert post_event(svc.port, "transcription.completed", "r0001", ts="t2") == 202
        assert wait_for(lambda: doc(rt, "r0001") is not None)
    finally:
        svc.stop()
    assert any("nope" in e["message"] for e in rt.counting_state.errors())


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"merged_recording_ids": ["a", "b"]}, [("a", "dst"), ("b", "dst")]),
        ({"recording": {"id": "dst", "merged_from": [{"id": "a"}]}}, [("a", "dst")]),
        ({"merged_into": "x"}, [("dst", "x")]),
        ({"recording": {"id": "dst", "merged_into_id": "x"}}, [("dst", "x")]),
        ({}, []),
    ],
)
def test_merge_ids(payload, expected):
    assert merge_ids("dst", payload) == expected


def test_queue_is_unbounded(rt):
    svc = make_service(rt)
    assert svc.queue.maxsize == 0


def _stop_soon(monkeypatch, module, delay=0.3):
    def fake_install(stop):
        threading.Timer(delay, stop.set).start()
        return lambda: None

    monkeypatch.setattr(module, "install_signal_handlers", fake_install)


def test_serve_verb_prepares_indexes_and_exits_0(rt, monkeypatch, capsys):
    created = []
    monkeypatch.setattr(vindex, "ensure_index", lambda col, m, d: created.append((m, d)) or [])
    _stop_soon(monkeypatch, serve_cmd, delay=0.5)
    assert main(["serve", "--port", "0", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["stopped"] is True and out["leftover_threads"] == []
    assert created == [("gemma2-embed", 768)]
    assert doc(rt, "r0001") is not None  # reconciler ran inside serve


def test_serve_refuses_vector_space_mismatch(rt, monkeypatch, capsys):
    rt.db[vindex.META_COLLECTION].insert_one({"_id": vindex.META_ID, "model": "old", "dims": 3})
    _stop_soon(monkeypatch, serve_cmd)
    assert main(["serve", "--port", "0"]) == 1
    err = capsys.readouterr().err
    assert "vector space mismatch" in err and "reembed --apply" in err


def test_serve_requires_webhook_secret(tmp_path, monkeypatch, capsys):
    install(monkeypatch, FakeRuntime(tmp_path, POCKETSYNC_WEBHOOK_SECRET=""))
    assert main(["serve", "--json"]) == 2
    assert "POCKETSYNC_WEBHOOK_SECRET" in json.loads(capsys.readouterr().err)["message"]


def test_watch_runs_reconciler_until_stopped(rt, monkeypatch, capsys):
    _stop_soon(monkeypatch, watch_cmd)
    assert main(["watch", "--interval", "0.05", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["stopped"] is True
    assert doc(rt, "r0001") is not None and doc(rt, "r0002") is not None
    assert rt.counting_state.get_watermark() is not None


def test_watch_rejects_bad_interval(rt, capsys):
    assert main(["watch", "--interval", "0"]) == 1
    assert "hint:" in capsys.readouterr().err


_CHILD = r"""
import sys, threading
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from pocketsync.cli._signals import run_until_signal
from tests.cli_fakes import FakeRuntime, SECRET, rec
from tests.test_cli_serve import make_service
rt = FakeRuntime(Path(sys.argv[2]), [rec(1)])
svc = make_service(rt)
stop = svc.stop_event
leftover = run_until_signal(svc, stop, on_started=lambda: print("ready", svc.port, flush=True))
print("stopped", len(leftover), flush=True)
sys.exit(0 if not leftover else 3)
"""


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_real_signal_stops_service_gracefully(tmp_path, sig):
    proc = subprocess.Popen(  # nosec B603 - fixed argv, our own interpreter
        [sys.executable, "-c", _CHILD, str(REPO), str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(REPO),
    )
    try:
        line = proc.stdout.readline()
        assert line.startswith("ready"), proc.stderr.read()
        proc.send_signal(sig)
        out, err = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, err
    assert "stopped 0" in out
