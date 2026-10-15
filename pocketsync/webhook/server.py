"""Loopback webhook receiver for Pocket: verify, dedupe, enqueue, ack fast.

The handler never calls Pocket, the gateway or a sink; landing happens on the
consumer of ``queue`` (wired by ``serve``). Request logging is suppressed so
bodies and headers never reach stderr.
"""

from __future__ import annotations

import logging
import queue as queue_mod
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from pocketsync.state import StateStore
from pocketsync.webhook import MalformedEvent, verify_and_parse

HOOK_PATH = "/hooks/pocket"
DEFAULT_MAX_BODY = 5 * 1024 * 1024
BODY_TIMEOUT = 10.0
_BAD_REQUEST = "bad request"
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

log = logging.getLogger("pocketsync.webhook")


def dedupe_string(key: tuple) -> str:
    return "|".join("" if part is None else str(part) for part in key)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    ctx: "WebhookServer"


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return  # no request logging: bodies/headers must not leak

    def _reply(self, status: int, text: str = "", *, close: bool = False) -> None:
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._not_post()

    do_PUT = do_DELETE = do_PATCH = do_GET  # noqa: N815

    def _not_post(self) -> None:
        if self.path.split("?", 1)[0] == HOOK_PATH:
            self._reply(405, "method not allowed", close=True)
        else:
            self._reply(404, "not found", close=True)

    def do_POST(self) -> None:  # noqa: N802
        ctx = self.server.ctx
        if self.path.split("?", 1)[0] != HOOK_PATH:
            self._reply(404, "not found", close=True)
            return
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            self._reply(411, "length required", close=True)
            return
        try:
            length = int(raw_len)
            if length < 0:
                raise ValueError
        except ValueError:
            self._reply(400, _BAD_REQUEST, close=True)
            return
        if length > ctx.max_body:
            self._reply(413, "payload too large", close=True)
            return
        self.connection.settimeout(BODY_TIMEOUT)
        try:
            raw = self.rfile.read(length)
        except OSError:
            self._reply(408, "request timeout", close=True)
            return
        if len(raw) != length:
            self._reply(400, _BAD_REQUEST, close=True)
            return
        self._handle_body(ctx, raw)

    def _handle_body(self, ctx: "WebhookServer", raw: bytes) -> None:
        try:
            result, event = verify_and_parse(
                dict(self.headers.items()), raw, ctx.secret, ctx.clock()
            )
        except MalformedEvent:
            ctx.state.record_error("webhook", "malformed event after valid signature")
            self._reply(400, _BAD_REQUEST)
            return
        if not result.ok or event is None:
            ctx.state.record_error("webhook", f"rejected: {result.reason}")
            self._reply(401, "unauthorized")
            return
        key = dedupe_string(event.dedupe_key)
        item = {
            "recording_id": event.recording_id,
            "event_type": event.event_type,
            "payload": event.payload,
        }
        if not ctx.state.mark_seen(key, item=item):
            self._reply(200, "duplicate")
            return
        ctx.state.set_last_webhook_at(datetime.fromtimestamp(ctx.clock(), timezone.utc).isoformat())
        # The dedupe key above doubles as a durable inbox entry: it stays pending until the
        # landing consumer has handled the event, so a crash, restart or failed handler is
        # replayed rather than Pocket's redelivery being dropped as a duplicate.
        try:
            ctx.queue.put_nowait((event.recording_id, event.event_type, event.payload, key))
        except queue_mod.Full:
            ctx.state.record_error("webhook", "queue full; event not enqueued")
            self._reply(503, "busy")
            return
        self._reply(202, "accepted")


class WebhookServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        secret: str,
        state: StateStore,
        queue: "queue_mod.Queue[Any]",
        max_body: int = DEFAULT_MAX_BODY,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if host not in _LOOPBACK:
            raise ValueError(f"webhook server may only bind loopback, not {host!r}")
        self.host = "127.0.0.1" if host == "localhost" else host
        self.port = port
        self.secret = secret
        self.state = state
        self.queue = queue
        self.max_body = max_body
        self.clock = clock
        self._httpd: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def server_address(self) -> tuple[Any, ...]:
        if self._httpd is None:
            raise RuntimeError("server not started")
        return self._httpd.server_address

    def start(self) -> None:
        httpd = _Server((self.host, self.port), _Handler)
        httpd.ctx = self
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
