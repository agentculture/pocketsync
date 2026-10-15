"""The default test run is offline: only loopback / AF_UNIX connections are allowed."""

from __future__ import annotations

import http.server
import socket
import threading
import time

import pytest

from tests.conftest import OfflineError


def test_outbound_connect_is_blocked_fast() -> None:
    start = time.monotonic()
    with pytest.raises(OfflineError, match="offline"):
        socket.create_connection(("192.0.2.1", 9), timeout=5)  # TEST-NET-1, never reached
    assert time.monotonic() - start < 1


def test_connect_and_connect_ex_are_blocked() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        with pytest.raises(OfflineError):
            s.connect(("192.0.2.1", 9))
        with pytest.raises(OfflineError):
            s.connect_ex(("192.0.2.1", 9))


def test_hostname_resolving_outside_loopback_is_blocked() -> None:
    with pytest.raises(OfflineError):
        socket.create_connection(("example.com", 443), timeout=5)


def test_ipv6_non_loopback_is_blocked() -> None:
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
        with pytest.raises(OfflineError):
            s.connect(("2001:db8::1", 9))


def test_loopback_still_works() -> None:
    server = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=2):
            pass
        with socket.create_connection(("localhost", server.server_address[1]), timeout=2):
            pass
    finally:
        server.shutdown()
        server.server_close()


def test_unix_sockets_still_work(tmp_path) -> None:
    path = str(tmp_path / "s.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.connect(path)
    finally:
        srv.close()
