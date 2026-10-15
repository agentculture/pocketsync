"""The real :class:`pocketsync.cli._runtime.Runtime` factory (offline: mongomock, loopback HTTP)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import mongomock
import pytest

from pocketsync import config
from pocketsync.cli import _runtime
from pocketsync.cli._errors import CliError
from pocketsync.enrich.gateway import GatewayClient
from pocketsync.events import Outbox, OutboxPublisher
from pocketsync.pocket.http import PocketHttpSource
from pocketsync.reconcile import Reconciler
from pocketsync.sinks.local import LocalSink
from pocketsync.sinks.mongo import MongoSink
from pocketsync.state import MongoStateStore


def settings(tmp_path, **env):
    base = {"POCKETSYNC_FILES_ROOT": str(tmp_path / "files")}
    base.update(env)
    return config.load(env=base)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://localhost:8001/v1", "http://localhost:8001/capabilities"),
        ("http://localhost:8001/v1/", "http://localhost:8001/capabilities"),
        ("http://localhost:8001", "http://localhost:8001/capabilities"),
    ],
)
def test_capabilities_url(url, expected):
    assert _runtime.capabilities_url(url) == expected


def test_source_requires_key_and_never_reprs_it(tmp_path):
    rt = _runtime.Runtime(settings(tmp_path))
    with pytest.raises(CliError) as exc:
        rt.source()
    assert exc.value.code == 2
    assert "POCKETSYNC_API_KEY" in exc.value.message
    rt = _runtime.Runtime(settings(tmp_path, POCKETSYNC_API_KEY="pk_synthetic"))
    src = rt.source()
    assert isinstance(src, PocketHttpSource)
    assert rt.source() is src
    assert "pk_synthetic" not in repr(src)


def test_mongo_pieces_share_one_database(tmp_path, monkeypatch):
    db = mongomock.MongoClient()["pocketsync"]
    seen = {}

    def fake_connect(uri, db_name="pocketsync", **kw):
        seen["kw"] = kw
        return db

    monkeypatch.setattr("pocketsync.sinks.mongo.connect", fake_connect)
    rt = _runtime.Runtime(
        settings(
            tmp_path, POCKETSYNC_MONGO_URI="mongodb://localhost:27017", POCKETSYNC_API_KEY="pk_x"
        )
    )
    assert isinstance(rt.store_sink(), MongoSink)
    assert isinstance(rt.state(), MongoStateStore)
    assert rt.database() is db
    assert seen["kw"]["serverSelectionTimeoutMS"] == _runtime.MONGO_TIMEOUT_MS
    rt.mongo_ping()  # mongomock answers ping
    assert isinstance(rt.files_sink(), LocalSink)
    assert isinstance(rt.reconciler(), Reconciler)
    assert isinstance(rt.outbox(), Outbox)
    assert isinstance(rt.publisher(), OutboxPublisher)
    assert isinstance(rt.gateway(), GatewayClient)
    assert rt.worker().concurrency == 2


def test_database_requires_uri(tmp_path):
    with pytest.raises(CliError) as exc:
        _runtime.Runtime(settings(tmp_path)).database()
    assert "POCKETSYNC_MONGO_URI" in exc.value.message


def test_files_root_expands_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    rt = _runtime.Runtime(config.load(env={"POCKETSYNC_FILES_ROOT": "~/landing"}))
    assert rt.files_root == tmp_path / "landing"


class _Caps(BaseHTTPRequestHandler):
    status = 200
    body = json.dumps({"embed": {"gemma2-embed": {"ready": True}}}).encode()

    def do_GET(self):  # noqa: N802
        if self.path != "/capabilities":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(self.status)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a):
        return


@pytest.fixture
def caps_server():
    srv = HTTPServer(("127.0.0.1", 0), _Caps)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_gateway_probe_reads_capabilities(tmp_path, caps_server):
    port = caps_server.server_address[1]
    rt = _runtime.Runtime(settings(tmp_path, POCKETSYNC_GATEWAY_URL=f"http://127.0.0.1:{port}/v1"))
    assert "gemma2-embed" in json.dumps(rt.gateway_probe())


def test_gateway_probe_errors_are_cli_errors(tmp_path, caps_server):
    port = caps_server.server_address[1]
    rt = _runtime.Runtime(
        settings(tmp_path, POCKETSYNC_GATEWAY_URL=f"http://127.0.0.1:{port}/nope/v1")
    )
    with pytest.raises(CliError, match="HTTP 404"):
        rt.gateway_probe()
    caps_server.shutdown()
    caps_server.server_close()
    rt = _runtime.Runtime(settings(tmp_path, POCKETSYNC_GATEWAY_URL=f"http://127.0.0.1:{port}/v1"))
    with pytest.raises(CliError, match="unreachable"):
        rt.gateway_probe()
    rt = _runtime.Runtime(settings(tmp_path, POCKETSYNC_GATEWAY_URL="file:///etc/v1"))
    with pytest.raises(CliError, match="http"):
        rt.gateway_probe()


def test_get_uses_current_build(monkeypatch, tmp_path):
    sentinel = _runtime.Runtime(settings(tmp_path))
    monkeypatch.setattr(_runtime, "build", lambda: sentinel)
    assert _runtime.get() is sentinel
