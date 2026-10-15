import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from pocketsync.cli._errors import CliError
from pocketsync.enrich.gateway import (
    EnrichmentUnavailable,
    GatewayClient,
    build_questions,
    load_taxonomy,
    taxonomy_from_dict,
)


class Fake(BaseHTTPRequestHandler):
    status = 200
    seen: list = []
    dims = 768

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Fake.seen.append((self.path, dict(self.headers), body))
        if Fake.status != 200:
            self.send_response(Fake.status)
            self.send_header("Retry-After", "5")
            self.end_headers()
            return
        if self.path.endswith("/embeddings"):
            n = len(body["input"])
            out = {"data": [{"index": i, "embedding": [0.1] * Fake.dims} for i in range(n)]}
        else:
            out = {
                "answers": {
                    "category": {
                        "type": "choice",
                        "choice": "meeting",
                        "confidence": 0.9,
                        "probabilities": {"meeting": 0.9, "call": 0.1},
                    },
                    "filter:has_action_items": {"type": "noul", "noul": 0.8},
                }
            }
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def client():
    Fake.status, Fake.seen, Fake.dims = 200, [], 768
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield GatewayClient(f"http://127.0.0.1:{srv.server_port}/v1", api_key="k")
    srv.shutdown()


def test_embed_request_shape(client):
    vecs = client.embed(["a", "b"])
    assert len(vecs) == 2
    assert len(vecs[0]) == 768
    path, headers, body = Fake.seen[0]
    assert path == "/v1/embeddings"
    assert body == {"model": "gemma2-embed", "input": ["a", "b"], "prompt_name": "Document"}
    assert headers["Authorization"] == "Bearer k"


def test_embed_wrong_dims(client, monkeypatch):
    monkeypatch.setattr(Fake, "dims", 128)
    with pytest.raises(CliError):
        client.embed(["a"])


def test_classify(client):
    res = client.classify("hello")
    path, headers, body = Fake.seen[0]
    assert path == "/v1/systemone"
    assert headers["X-Lobes-Model"] == "decider:general"
    assert body["state"] == "hello"
    assert set(body["questions"]["category"]["criteria"]) >= {"meeting", "other", "idea"}
    assert res.label == "meeting"
    assert res.confidence == 0.9
    assert res.filters == {"has_action_items": 0.8}
    assert res.taxonomy_version == client.taxonomy.version


@pytest.mark.parametrize("status", [503, 404, 429, 500, 502, 504])
def test_unavailable_is_retryable(client, status, monkeypatch):
    monkeypatch.setattr(Fake, "status", status)
    with pytest.raises(EnrichmentUnavailable) as ei:
        client.embed(["a"])
    assert ei.value.retryable
    assert ei.value.retry_after == "5"


def test_unreachable_is_retryable():
    unreachable = GatewayClient("http://127.0.0.1:1/v1", timeout=1)
    with pytest.raises(EnrichmentUnavailable):
        unreachable.embed(["a"])


def test_taxonomies_differ():
    default = load_taxonomy()
    other = taxonomy_from_dict(
        {"labels": [{"name": "x", "description": "d"}], "filters": [], "multi": True}
    )
    assert default.version != other.version
    assert build_questions(default) != build_questions(other)
    assert len(default.version) == 64
    assert build_questions(other) == {
        "label:x": {"type": "noul", "instructions": "What kind of recording is this? Is it: d?"}
    }


def test_load_taxonomy_from_path(tmp_path):
    p = tmp_path / "t.toml"
    p.write_text('[[labels]]\nname = "a"\ndescription = "b"\n')
    assert [x.name for x in load_taxonomy(p).labels] == ["a"]
    with pytest.raises(CliError):
        load_taxonomy(tmp_path / "missing.toml")


@pytest.mark.parametrize("status", [400, 401, 422])
def test_client_errors_are_not_retryable(client, status, monkeypatch):
    monkeypatch.setattr(Fake, "status", status)
    with pytest.raises(CliError) as ei:
        client.embed(["a"])
    assert not isinstance(ei.value, EnrichmentUnavailable)


def test_default_taxonomy_is_the_approved_q8_set():
    # q8 (operator decision 2026-10-15): keep the default labels, single-label, and add a
    # sensitive/private filter so downstream routing can keep such recordings out of shared
    # destinations.
    tax = load_taxonomy()
    assert [lab.name for lab in tax.labels] == [
        "meeting",
        "call",
        "idea",
        "todo-reminder",
        "personal-note",
        "lecture-learning",
        "other",
    ]
    assert tax.multi is False
    assert [f.name for f in tax.filters] == ["has_action_items", "sensitive"]


def test_unanswered_filter_is_absent_not_zero(client):
    # A missing decider answer is "unknown". Recording it as 0.0 would read as "not sensitive"
    # and could let a private recording be routed to a shared destination.
    res = client.classify("hello")  # the fake answers has_action_items only
    assert "sensitive" not in res.filters
