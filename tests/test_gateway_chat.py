import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from pocketsync.enrich.gateway import (
    CHAT_MODEL,
    EnrichmentUnavailable,
    GatewayClient,
)


class FakeChat(BaseHTTPRequestHandler):
    status = 200
    content = "hello"
    seen: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeChat.seen.append((self.path, body))
        if FakeChat.status != 200:
            self.send_response(FakeChat.status)
            self.end_headers()
            return
        if self.path.endswith("/embeddings"):
            out = {"data": [{"index": 0, "embedding": [0.1] * 768}]}
        else:
            out = {"choices": [{"message": {"role": "assistant", "content": FakeChat.content}}]}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def client():
    FakeChat.status, FakeChat.content, FakeChat.seen = 200, "hello", []
    srv = HTTPServer(("127.0.0.1", 0), FakeChat)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield GatewayClient(f"http://127.0.0.1:{srv.server_port}/v1", api_key="k")
    srv.shutdown()


MSGS = [{"role": "user", "content": "hi"}]


def test_embed_query_prompt_name(client):
    client.embed(["q"], prompt_name="Query")
    assert FakeChat.seen[0][1]["prompt_name"] == "Query"


def test_embed_default_prompt_name_unchanged(client):
    client.embed(["q"])
    assert FakeChat.seen[0][1]["prompt_name"] == "Document"


def test_chat_request_shape(client):
    assert client.chat(MSGS, max_tokens=50) == "hello"
    path, body = FakeChat.seen[0]
    assert path == "/v1/chat/completions"
    assert body["model"] == CHAT_MODEL == "unsloth/Qwen3.8-27B-NVFP4"
    assert body["messages"] == MSGS
    assert body["max_tokens"] == 50
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert "tools" not in body
    assert "functions" not in body
    assert "tool_choice" not in body
    assert "function_call" not in body


def test_chat_model_override(client):
    client.chat(MSGS, model="other-model")
    assert FakeChat.seen[0][1]["model"] == "other-model"
    client.chat_model = "instance-model"
    client.chat(MSGS)
    assert FakeChat.seen[1][1]["model"] == "instance-model"


def test_chat_model_constructor():
    assert GatewayClient("http://127.0.0.1:1/v1").chat_model == CHAT_MODEL
    assert GatewayClient("http://127.0.0.1:1/v1", chat_model="m").chat_model == "m"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("<think>reasoning\nmore</think>\n\nThe answer.", "The answer."),
        ("The answer.", "The answer."),
        ("<think>a</think>X<think>b</think> Y", "X Y"),
        ("</think>\nAnswer after stray close", "Answer after stray close"),
    ],
)
def test_chat_strips_think(client, raw, expected, monkeypatch):
    monkeypatch.setattr(FakeChat, "content", raw)
    assert client.chat(MSGS) == expected


@pytest.mark.parametrize("status", [404, 429, 500, 502, 503, 504])
def test_chat_unavailable(client, status, monkeypatch):
    monkeypatch.setattr(FakeChat, "status", status)
    with pytest.raises(EnrichmentUnavailable):
        client.chat(MSGS)


def test_chat_unreachable():
    unreachable = GatewayClient("http://127.0.0.1:1/v1", timeout=1)
    with pytest.raises(EnrichmentUnavailable):
        unreachable.chat(MSGS)


def test_default_chat_model_has_one_source():
    from pocketsync import config
    from pocketsync.enrich import gateway

    assert gateway.CHAT_MODEL is config.DEFAULT_CHAT_MODEL
