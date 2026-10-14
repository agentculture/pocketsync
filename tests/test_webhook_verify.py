"""Tests for pocketsync.webhook.verify (synthetic data only)."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from pocketsync.webhook import (
    MalformedEvent,
    parse_event,
    verify,
    verify_and_parse,
)

SECRET = "not-a-real-secret"  # synthetic
NOW = 1_700_000_000.0
BODY = json.dumps(
    {
        "event": "summary.completed",
        "timestamp": "2023-11-14T22:13:20Z",
        "recording": {"id": "rec-1"},
    }
).encode()


def sign(ts: str, body: bytes = BODY, secret: str = SECRET) -> str:
    msg = ts.encode() + b"." + body
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def headers(ts: str, sig: str | None = None, **extra: str) -> dict[str, str]:
    h = {"X-HeyPocket-Timestamp": ts}
    h["X-HeyPocket-Signature"] = sign(ts) if sig is None else sig
    h.update(extra)
    return h


def run(h, body=BODY, secret=SECRET, now=NOW, tolerance=300.0):
    return verify(h, body, secret, now, tolerance)


def test_valid_ms_timestamp() -> None:
    r = run(headers(str(int(NOW * 1000))))
    assert r.ok and r.reason == "ok"


def test_valid_seconds_timestamp() -> None:
    assert run(headers(str(int(NOW)))).ok


def test_sha256_prefix_form() -> None:
    ts = str(int(NOW * 1000))
    assert run(headers(ts, "sha256=" + sign(ts))).ok


def test_uppercase_hex_and_header_case_insensitive() -> None:
    ts = str(int(NOW))
    h = {"x-heypocket-timestamp": ts, "X-HEYPOCKET-SIGNATURE": sign(ts).upper()}
    assert run(h).ok


def test_timestamp_used_exactly_as_received() -> None:
    ts = str(int(NOW)) + ".0"
    assert run(headers(ts)).ok


def test_missing_signature() -> None:
    r = run({"X-HeyPocket-Timestamp": str(int(NOW))})
    assert not r.ok and r.reason == "missing_signature"


def test_empty_signature_is_missing() -> None:
    assert run(headers(str(int(NOW)), "")).reason == "missing_signature"


def test_missing_timestamp() -> None:
    r = run({"X-HeyPocket-Signature": "abc"})
    assert r.reason == "missing_timestamp"


def test_malformed_timestamp() -> None:
    assert run(headers("yesterday")).reason == "malformed_timestamp"


def test_stale_ms_and_seconds() -> None:
    assert run(headers(str(int((NOW - 301) * 1000)))).reason == "stale"
    assert run(headers(str(int(NOW - 301)))).reason == "stale"


def test_future_timestamp_is_stale() -> None:
    assert run(headers(str(int(NOW + 301)))).reason == "stale"


def test_within_tolerance_edge() -> None:
    assert run(headers(str(int(NOW - 299)))).ok


def test_tampered_body() -> None:
    h = headers(str(int(NOW)))
    r = run(h, body=BODY + b" ")
    assert r.reason == "bad_signature"


def test_wrong_secret() -> None:
    assert run(headers(str(int(NOW))), secret="other").reason == "bad_signature"


def test_non_hex_signature_is_bad_signature() -> None:
    assert run(headers(str(int(NOW)), "zzé")).reason == "bad_signature"


def test_unsigned_check_precedes_stale() -> None:
    r = run({"X-HeyPocket-Timestamp": "1"})
    assert r.reason == "missing_signature"


def test_verify_does_not_parse_body() -> None:
    ts = str(int(NOW))
    body = b"\xff not json"
    assert verify(
        {"X-HeyPocket-Timestamp": ts, "X-HeyPocket-Signature": sign(ts, body)}, body, SECRET, NOW
    ).ok


def test_parse_event_tuple() -> None:
    ev, rid, ets, key = parse_event(BODY)
    assert (ev, rid, ets) == ("summary.completed", "rec-1", "2023-11-14T22:13:20Z")
    assert key == ("summary.completed", "rec-1", "2023-11-14T22:13:20Z")


@pytest.mark.parametrize("raw", [b"nope", b"[]", b'{"recording": {}}'])
def test_parse_event_malformed(raw: bytes) -> None:
    with pytest.raises(MalformedEvent):
        parse_event(raw)


def test_verify_and_parse_ok_keeps_payload() -> None:
    res, ev = verify_and_parse(headers(str(int(NOW))), BODY, SECRET, NOW)
    assert res.ok and ev is not None
    assert ev.recording_id == "rec-1" and ev.payload["recording"] == {"id": "rec-1"}


def test_verify_and_parse_never_parses_unverified(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    mod = sys.modules["pocketsync.webhook.verify"]

    def boom(*a, **k):
        raise AssertionError("parsed before verification")

    monkeypatch.setattr(mod, "parse_payload", boom)
    res, ev = verify_and_parse(headers(str(int(NOW)), "0" * 64), BODY, SECRET, NOW)
    assert not res.ok and ev is None


def test_verify_and_parse_signed_garbage_raises() -> None:
    ts = str(int(NOW))
    body = b"garbage"
    h = {"X-HeyPocket-Timestamp": ts, "X-HeyPocket-Signature": sign(ts, body)}
    with pytest.raises(MalformedEvent):
        verify_and_parse(h, body, SECRET, NOW)
