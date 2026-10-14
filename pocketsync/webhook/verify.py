"""Verify Pocket webhook signatures and parse events. Pure: no I/O, no server.

Pocket signs ``"{timestamp}.{raw_body}"`` with HMAC-SHA256 (hex) keyed by the
webhook secret and sends it in ``X-HeyPocket-Signature`` alongside
``X-HeyPocket-Timestamp``. Real senders sometimes use seconds instead of
milliseconds and a ``sha256=<hex>`` signature form; both are accepted. The
timestamp header is signed exactly as received.

Result contract: :func:`verify` never raises for bad input; it returns a
:class:`VerifyResult` whose ``reason`` is one of ``ok``, ``missing_signature``,
``missing_timestamp``, ``malformed_timestamp``, ``stale``, ``bad_signature``.
Parsing is structurally separate: ``verify`` never touches JSON, and
:func:`verify_and_parse` parses only after verification succeeded.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

SIGNATURE_HEADER = "X-HeyPocket-Signature"
TIMESTAMP_HEADER = "X-HeyPocket-Timestamp"
DEFAULT_TOLERANCE = 300.0
# Values at or above this are milliseconds (1e11 s is year 5138; 1e11 ms is 1973).
_MS_THRESHOLD = 1e11


class MalformedEvent(ValueError):
    """Raised when a payload is not a valid webhook event."""


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    reason: str


@dataclass(frozen=True)
class WebhookEvent:
    event_type: str
    recording_id: str | None
    event_timestamp: str | None
    dedupe_key: tuple[str, str | None, str | None]
    payload: dict[str, Any]


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def verify(
    headers: Mapping[str, str],
    raw_body: bytes,
    secret: str,
    now: float,
    tolerance: float = DEFAULT_TOLERANCE,
) -> VerifyResult:
    """Check signature and freshness. ``now`` is Unix seconds. Never parses the body."""
    sig = (_header(headers, SIGNATURE_HEADER) or "").strip()
    if not sig:
        return VerifyResult(False, "missing_signature")
    ts_raw = _header(headers, TIMESTAMP_HEADER)
    if ts_raw is None or not ts_raw.strip():
        return VerifyResult(False, "missing_timestamp")
    try:
        ts_val = float(ts_raw.strip())
    except ValueError:
        return VerifyResult(False, "malformed_timestamp")
    if not math.isfinite(ts_val) or ts_val < 0:
        return VerifyResult(False, "malformed_timestamp")
    ts_seconds = ts_val / 1000.0 if ts_val >= _MS_THRESHOLD else ts_val
    if abs(now - ts_seconds) > tolerance:
        return VerifyResult(False, "stale")

    if sig.lower().startswith("sha256="):
        sig = sig[len("sha256=") :]
    message = ts_raw.encode() + b"." + raw_body
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode(), sig.lower().encode()):
        return VerifyResult(False, "bad_signature")
    return VerifyResult(True, "ok")


def parse_payload(raw_body: bytes) -> dict[str, Any]:
    """Decode the JSON object payload. Call only after :func:`verify` succeeded."""
    try:
        data = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise MalformedEvent("body is not valid JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("event"), str):
        raise MalformedEvent("payload is not an object with an 'event' string")
    return data


def _event_from_payload(data: dict[str, Any]) -> WebhookEvent:
    recording = data.get("recording")
    rid = recording.get("id") if isinstance(recording, dict) else None
    rid = None if rid is None else str(rid)
    ts = data.get("timestamp")
    ts = None if ts is None else str(ts)
    event = data["event"]
    return WebhookEvent(event, rid, ts, (event, rid, ts), data)


def parse_event(raw_body: bytes) -> tuple[str, str | None, str | None, tuple]:
    """Return ``(event_type, recording_id, event_timestamp, dedupe_key)``.

    Must only be called on a body that already passed :func:`verify`.
    """
    ev = _event_from_payload(parse_payload(raw_body))
    return ev.event_type, ev.recording_id, ev.event_timestamp, ev.dedupe_key


def verify_and_parse(
    headers: Mapping[str, str],
    raw_body: bytes,
    secret: str,
    now: float,
    tolerance: float = DEFAULT_TOLERANCE,
) -> tuple[VerifyResult, WebhookEvent | None]:
    """Verify, and only on success parse. Raises MalformedEvent for a signed bad body."""
    result = verify(headers, raw_body, secret, now, tolerance)
    if not result.ok:
        return result, None
    return result, _event_from_payload(parse_payload(raw_body))
