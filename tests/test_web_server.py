"""``pocketsync web`` server: loopback, Access-gated, JSON API over the verb registry.

Every test runs the real :class:`pocketsync.web.server.WebServer` on an ephemeral loopback
port against :class:`tests.cli_fakes.FakeRuntime` (mongomock, fake gateway, fake chat) and a
fake JWKS fetcher with a synthetic RSA key. Nothing leaves loopback.
"""

from __future__ import annotations

import http.client
import json
import logging
import threading
from typing import Any

import pytest

from pocketsync import verbs
from pocketsync.cli._errors import CliError
from pocketsync.web import server as web_server
from pocketsync.web.access import AccessVerifier
from tests.cli_fakes import FakeRuntime
from tests.test_cli_recall import _doc
from tests.test_web_access import AUD, NOW, TEAM, claims, jwks, keypair, token

ORIGIN = "https://pocketchat.example.test"
CSP = (
    "default-src 'self'; img-src 'self' data:; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)

# -- fixtures --------------------------------------------------------------------------------


def make_verifier(fetch=None) -> AccessVerifier:
    return AccessVerifier(TEAM, AUD, fetch_jwks=fetch or (lambda: jwks()), clock=lambda: float(NOW))


def seed(runtime: FakeRuntime) -> None:
    runtime.db["recordings"].insert_many(
        [
            _doc(
                "r0001",
                "2026-10-01T09:00:00Z",
                actions=[{"text": "send notes", "done": False, "owner": "Dana"}],
            ),
            _doc("r0002", "2026-10-05T09:00:00Z", labels=("personal",)),
            _doc(
                "r0003",
                "2026-10-09T09:00:00Z",
                segments=[
                    {
                        "start": 0.0,
                        "end": 3.0,
                        "text": "<script>alert(1)</script><img src=x onerror=y>",
                        "speaker": "A",
                    }
                ],
            ),
        ]
    )


@pytest.fixture
def static_dir(tmp_path):
    d = tmp_path / "static"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>synthetic page</title>")
    (d / "app.css").write_text("body{color:black}")
    (d / "app.js").write_text("// synthetic")
    (d / "icon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    return d


class Client:
    """A tiny loopback HTTP client; ``auth`` adds a valid Access JWT."""

    def __init__(self, port: int) -> None:
        self.port = port

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        auth: bool = True,
        jwt: str | None = None,
        headers: dict[str, str] | None = None,
        raw: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        hdrs: dict[str, str] = {}
        if jwt is not None:
            hdrs["Cf-Access-Jwt-Assertion"] = jwt
        elif auth:
            hdrs["Cf-Access-Jwt-Assertion"] = token()
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
            hdrs.setdefault("Origin", ORIGIN)
        hdrs.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        finally:
            conn.close()

    def json(self, method: str, path: str, body: Any = None, **kw: Any) -> tuple[int, Any]:
        status, _, data = self.request(method, path, body, **kw)
        return status, json.loads(data)


@pytest.fixture
def runtime(tmp_path):
    rt = FakeRuntime(tmp_path, POCKETSYNC_SITE_ORIGIN=ORIGIN)
    seed(rt)
    return rt


@pytest.fixture
def web(runtime, static_dir):
    srv = web_server.WebServer(runtime, port=0, verifier=make_verifier(), static_dir=static_dir)
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture
def client(web):
    return Client(web.port)


def no_vectors(value: Any) -> bool:
    if isinstance(value, dict):
        return all("vector" not in k and no_vectors(v) for k, v in value.items())
    if isinstance(value, list):
        return all(no_vectors(v) for v in value)
    return True


# -- binding ---------------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::"])  # nosec B104 - refused
def test_refuses_non_loopback_bind(runtime, host):
    with pytest.raises(ValueError):
        web_server.WebServer(runtime, host=host, port=0, verifier=None)


def test_default_static_dir_ships_index_html():
    assert (web_server.STATIC_DIR / "index.html").is_file()


# -- static files and headers ----------------------------------------------------------------


def test_get_root_serves_index_with_security_headers(client):
    status, headers, body = client.request("GET", "/")
    assert status == 200
    assert body == b"<!doctype html><title>synthetic page</title>"
    assert headers["content-type"].startswith("text/html")
    assert headers["content-security-policy"] == CSP
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize(
    "path, ctype",
    [
        ("/static/app.css", "text/css"),
        ("/static/app.js", "text/javascript"),
        ("/static/icon.svg", "image/svg+xml"),
    ],
)
def test_static_assets(client, path, ctype):
    status, headers, _ = client.request("GET", path)
    assert status == 200
    assert headers["content-type"].startswith(ctype)
    assert headers["content-security-policy"] == CSP


@pytest.mark.parametrize(
    "path", ["/static/missing.js", "/static/../index.html", "/static/%2e%2e/x", "/nope"]
)
def test_unknown_paths_are_404_with_headers(client, path):
    status, headers, body = client.request("GET", path)
    assert status == 404
    assert json.loads(body)["error"]["code"] == "not_found"
    assert headers["content-security-policy"] == CSP


def test_api_responses_are_no_store_json(client):
    status, headers, _ = client.request("GET", "/api/health")
    assert status == 200
    assert headers["content-type"].startswith("application/json")
    assert headers["cache-control"] == "no-store"
    assert headers["content-security-policy"] == CSP


# -- Access ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "jwt",
    [
        None,  # missing
        "not.a.jwt",
        token(claims(exp=NOW - 1)),  # expired
        token(claims(aud=["some-other-app"])),  # wrong audience
        token(pair=keypair("kid-1", seed=99)),  # forged: same kid, another key
    ],
    ids=["missing", "malformed", "expired", "wrong-aud", "forged"],
)
@pytest.mark.parametrize("path", ["/", "/api/health", "/api/recordings"])
def test_bad_or_missing_jwt_is_403(client, jwt, path):
    if jwt is None:
        status, body = client.json("GET", path, auth=False)
    else:
        status, body = client.json("GET", path, jwt=jwt)
    assert status == 403
    assert body["error"]["code"] == "forbidden"
    assert set(body["error"]) == {"code", "message", "hint"}


def test_jwks_unavailable_is_503(runtime, static_dir):
    def down():
        raise OSError("synthetic JWKS outage")

    srv = web_server.WebServer(
        runtime, port=0, verifier=make_verifier(fetch=down), static_dir=static_dir
    )
    srv.start()
    try:
        status, body = Client(srv.port).json("GET", "/api/health")
    finally:
        srv.stop()
    assert status == 503
    assert body["error"]["code"] == "unavailable"


def test_no_verifier_means_no_access_check(runtime, static_dir):
    srv = web_server.WebServer(runtime, port=0, verifier=None, static_dir=static_dir)
    srv.start()
    try:
        status, body = Client(srv.port).json("GET", "/api/health", auth=False)
    finally:
        srv.stop()
    assert status == 200 and body["ok"] is True


# -- read endpoints --------------------------------------------------------------------------


def test_health(client, runtime):
    status, body = client.json("GET", "/api/health")
    assert status == 200
    assert body["ok"] is True
    assert body["chat_model"] == runtime.settings.chat_model
    assert isinstance(body["version"], str)


def test_recordings_list_with_query(client):
    status, body = client.json("GET", "/api/recordings?since=&until=&label=meeting&limit=10")
    assert status == 200
    assert [r["id"] for r in body["recordings"]] == ["r0003", "r0001"]
    assert no_vectors(body)


@pytest.mark.parametrize("query", ["limit=abc", "limit=0", "bogus=1", "exclude_sensitive=maybe"])
def test_recordings_bad_query_is_400(client, query):
    status, body = client.json("GET", f"/api/recordings?{query}")
    assert status == 400
    assert body["error"]["code"] == "bad_request"
    assert body["error"]["hint"]


def test_get_recording_and_404(client):
    status, body = client.json("GET", "/api/recordings/r0001")
    assert status == 200 and body["id"] == "r0001"
    assert no_vectors(body)
    status, body = client.json("GET", "/api/recordings/r9999")
    assert status == 404
    assert body["error"]["code"] == "not_found"


def test_transcript_window(client):
    status, body = client.json("GET", "/api/recordings/r0001/transcript?start=6&end=9")
    assert status == 200
    assert [s["text"] for s in body["segments"]] == ["we decided on budgets"]
    assert (body["start"], body["end"]) == (6.0, 9.0)


def test_html_bearing_transcript_round_trips_as_json_text(client):
    status, headers, raw = client.request("GET", "/api/recordings/r0003/transcript")
    assert status == 200
    assert headers["content-type"].startswith("application/json")
    assert headers["x-content-type-options"] == "nosniff"
    body = json.loads(raw)
    assert body["segments"][0]["text"] == "<script>alert(1)</script><img src=x onerror=y>"


def test_actions(client):
    status, body = client.json("GET", "/api/actions?open_only=true")
    assert status == 200
    assert [a["text"] for a in body["action_items"]] == ["send notes"]


# -- POST endpoints --------------------------------------------------------------------------


def test_search_post(client):
    status, body = client.json(
        "POST", "/api/search", {"query": "budgets", "labels": ["meeting"], "limit": 5}
    )
    assert status == 200
    assert body["mode"] == "vector"
    assert {r["id"] for r in body["results"]} == {"r0001", "r0003"}
    assert no_vectors(body)


def test_search_rejects_both_label_spellings(client):
    status, body = client.json(
        "POST", "/api/search", {"query": "x", "labels": ["a"], "label": ["b"]}
    )
    assert status == 400


def test_enrich_dry_run_then_apply(client, runtime):
    status, body = client.json("POST", "/api/enrich", {"ids": ["r0001"], "only": "classify"})
    assert status == 200
    assert body["dry_run"] is True and body["ids"] == ["r0001"] and body["updated"] == 0
    assert runtime.store.writes == 0
    status, body = client.json(
        "POST", "/api/enrich", {"ids": ["r0001"], "only": "classify", "apply": True}
    )
    assert status == 200
    assert body["dry_run"] is False and body["updated"] == 1


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Type": "text/plain"},
        {"Content-Type": "application/x-www-form-urlencoded"},
        {"Origin": "https://evil.example.test"},
        {"Origin": "null"},
    ],
)
@pytest.mark.parametrize("path", ["/api/search", "/api/chat", "/api/enrich"])
def test_post_needs_json_and_same_origin(client, runtime, headers, path):
    body = {"ids": ["r0001"], "apply": True} if path == "/api/enrich" else {"query": "x"}
    status, resp = client.json("POST", path, body, headers=headers)
    assert status == 403
    assert resp["error"]["code"] == "forbidden"
    assert runtime.store.writes == 0
    assert runtime.fake_chat.calls == []


def test_post_without_origin_is_403(client):
    status, _, _ = client.request(
        "POST",
        "/api/search",
        raw=b'{"query": "x"}',
        headers={"Content-Type": "application/json"},
    )
    assert status == 403


def test_json_content_type_with_charset_is_accepted(client):
    status, _ = client.json(
        "POST",
        "/api/search",
        {"query": "budgets"},
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    assert status == 200


def test_body_too_large_is_413(client):
    big = {"query": "x" * (web_server.MAX_BODY + 10)}
    status, body = client.json("POST", "/api/search", big)
    assert status == 413


@pytest.mark.parametrize("raw", [b"{not json", b"[1, 2]", b'"text"'])
def test_bad_json_is_400(client, raw):
    status, _, data = client.request(
        "POST",
        "/api/search",
        raw=raw,
        headers={"Content-Type": "application/json", "Origin": ORIGIN},
    )
    assert status == 400
    assert json.loads(data)["error"]["code"] == "bad_request"


@pytest.mark.parametrize(
    "method, path", [("GET", "/api/search"), ("POST", "/api/recordings"), ("DELETE", "/")]
)
def test_wrong_method_is_405(client, method, path):
    status, _, _ = client.request(
        method, path, raw=b"{}", headers={"Content-Type": "application/json", "Origin": ORIGIN}
    )
    assert status == 405


# -- errors ----------------------------------------------------------------------------------


def test_missing_mongo_is_503(tmp_path, static_dir):
    rt = FakeRuntime(tmp_path, POCKETSYNC_MONGO_URI="", POCKETSYNC_SITE_ORIGIN=ORIGIN)
    srv = web_server.WebServer(rt, port=0, verifier=make_verifier(), static_dir=static_dir)
    srv.start()
    try:
        status, body = Client(srv.port).json("GET", "/api/recordings")
    finally:
        srv.stop()
    assert status == 503
    assert body["error"]["code"] == "unavailable"


def test_unexpected_error_is_500_without_detail(client, monkeypatch):
    def boom(self, runtime, arguments=None):
        raise RuntimeError("synthetic secret detail")

    monkeypatch.setattr(verbs.Verb, "invoke", boom)
    status, _, raw = client.request("GET", "/api/recordings")
    assert status == 500
    body = json.loads(raw)
    assert body["error"]["code"] == "internal"
    assert b"synthetic secret detail" not in raw and b"Traceback" not in raw


# -- every endpoint goes through Verb.invoke -------------------------------------------------


@pytest.mark.parametrize(
    "method, path, body, verb",
    [
        ("GET", "/api/recordings", None, "recordings"),
        ("GET", "/api/recordings/r0001", None, "get"),
        ("GET", "/api/recordings/r0001/transcript", None, "transcript"),
        ("GET", "/api/actions", None, "actions"),
        ("POST", "/api/search", {"query": "budgets"}, "search"),
        ("POST", "/api/enrich", {"ids": ["r0001"]}, "enrich"),
        ("POST", "/api/chat", {"question": "budgets?"}, "search"),
    ],
)
def test_endpoints_call_registry_verbs(client, monkeypatch, method, path, body, verb):
    called: list[str] = []
    real = verbs.Verb.invoke

    def spy(self, runtime, arguments=None):
        called.append(self.name)
        return real(self, runtime, arguments)

    monkeypatch.setattr(verbs.Verb, "invoke", spy)
    status, _ = client.json(method, path, body)
    assert status == 200
    assert called == [verb]


# -- access log ------------------------------------------------------------------------------


def test_access_log_has_method_path_status_duration_only(client, caplog):
    caplog.set_level(logging.INFO, logger=web_server.ACCESS_LOGGER)
    secret_question = "what did we decide about the synthetic-zebra budget?"
    client.json("POST", "/api/chat", {"question": secret_question})
    client.json("GET", "/api/recordings?label=synthetic-query-marker")
    lines = [r.getMessage() for r in caplog.records if r.name == web_server.ACCESS_LOGGER]
    assert len(lines) == 2
    assert lines[0].startswith("POST /api/chat 200 ") and lines[0].endswith("ms")
    assert lines[1].startswith("GET /api/recordings 200 ")
    text = "\n".join(lines)
    assert "zebra" not in text and "synthetic-query-marker" not in text
    assert "Synthetic answer" not in text


# -- serve() ---------------------------------------------------------------------------------


def test_serve_refuses_without_access_config(runtime):
    with pytest.raises(CliError) as ei:
        web_server.serve(runtime, port=0, access_check=True)
    assert ei.value.code == 2
    hint = ei.value.remediation
    assert "POCKETSYNC_ACCESS_TEAM_DOMAIN" in hint and "POCKETSYNC_ACCESS_AUD" in hint
    assert "--no-access-check" in hint


def test_serve_refuses_partial_access_config(tmp_path):
    rt = FakeRuntime(tmp_path, POCKETSYNC_ACCESS_TEAM_DOMAIN=TEAM)
    with pytest.raises(CliError) as ei:
        web_server.serve(rt, port=0, access_check=True)
    assert ei.value.code == 2
    assert "POCKETSYNC_ACCESS_AUD" in ei.value.message + ei.value.remediation


def _fake_run(seen: dict[str, Any]):
    def run(service, stop, *, on_started=None, install=None):
        service.start()
        try:
            if on_started is not None:
                on_started()
            seen["verifier"] = service.verifier
            seen["port"] = service.port
            seen["result"] = Client(service.port).json("GET", "/api/health", auth=False)
        finally:
            leftover = service.stop()
        return leftover

    return run


def test_serve_no_access_check_warns_loudly_and_serves(runtime, monkeypatch, capsys):
    seen: dict[str, Any] = {}
    monkeypatch.setattr(web_server, "run_until_signal", _fake_run(seen))
    assert web_server.serve(runtime, port=0, access_check=False) == 0
    err = capsys.readouterr().err
    assert "WARNING" in err and "Access" in err
    assert seen["verifier"] is None
    assert seen["result"][0] == 200


def test_serve_with_access_config_verifies(tmp_path, monkeypatch):
    rt = FakeRuntime(tmp_path, POCKETSYNC_ACCESS_TEAM_DOMAIN=TEAM, POCKETSYNC_ACCESS_AUD=AUD)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(web_server, "run_until_signal", _fake_run(seen))
    assert web_server.serve(rt, port=0, access_check=True) == 0
    assert isinstance(seen["verifier"], AccessVerifier)
    assert seen["verifier"].audience == AUD
    # an unauthenticated request is refused (the JWKS fetch never happens: no token)
    assert seen["result"][0] == 403


def test_stop_returns_leftover_threads(runtime, static_dir):
    srv = web_server.WebServer(runtime, port=0, verifier=None, static_dir=static_dir)
    srv.start()
    assert srv.stop() == []
    assert not any(t.name.startswith("pocketsync-web") for t in threading.enumerate())


def test_refused_post_keeps_the_connection_usable(web):
    conn = http.client.HTTPConnection("127.0.0.1", web.port, timeout=10)
    try:
        hdrs = {"Cf-Access-Jwt-Assertion": token(), "Content-Type": "text/plain"}
        conn.request("POST", "/api/search", body=b'{"query": "x"}', headers=hdrs)
        first = conn.getresponse()
        first.read()
        assert first.status == 403
        conn.request("GET", "/api/health", headers={"Cf-Access-Jwt-Assertion": token()})
        second = conn.getresponse()
        assert second.status == 200 and json.loads(second.read())["ok"] is True
    finally:
        conn.close()


# -- the CLI verb reaches serve() ------------------------------------------------------------


def test_cli_web_refuses_without_access_config(tmp_path, monkeypatch, capsys):
    from pocketsync.cli import main
    from tests.cli_fakes import install

    install(monkeypatch, FakeRuntime(tmp_path))
    assert main(["web", "--json"]) == 2
    err = json.loads(capsys.readouterr().err)
    assert "--no-access-check" in err["remediation"]


def test_cli_web_no_access_check_serves(tmp_path, monkeypatch, capsys):
    from pocketsync.cli import main
    from tests.cli_fakes import install

    install(monkeypatch, FakeRuntime(tmp_path))
    seen: dict[str, Any] = {}
    monkeypatch.setattr(web_server, "run_until_signal", _fake_run(seen))
    assert main(["web", "--port", "0", "--no-access-check"]) == 0
    assert seen["result"][0] == 200
    assert "WARNING" in capsys.readouterr().err
