"""Shared pytest configuration: the default run is offline.

``socket.socket.connect`` / ``connect_ex`` and ``socket.create_connection`` are
patched for the whole session (including pytest-xdist workers, which each load
this conftest) so that only loopback (127.0.0.0/8, ::1, ``localhost``) and
AF_UNIX connections succeed. Anything else raises :class:`OfflineError` before
any packet is sent or any DNS lookup is attempted. Opt-in integration tests
(``tests/integration``) talk to a loopback MongoDB, so they are unaffected.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Any

# Never re-exec into the operator's real grant store from a test (in-process main() or a
# `python -m pocketsync` child): see pocketsync/cli/_grant.py.
os.environ["POCKETSYNC_NO_GRANT"] = "1"

_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex
_REAL_CREATE_CONNECTION = socket.create_connection


class OfflineError(OSError):
    """Raised when a test tries to reach a non-loopback address."""


def _is_loopback_host(host: Any) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    host = host.strip("[]").split("%", 1)[0].lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_loopback


def _check(sock: socket.socket, address: Any) -> None:
    if sock.family == getattr(socket, "AF_UNIX", object()):
        return
    host = address[0] if isinstance(address, tuple) and address else address
    if not _is_loopback_host(host):
        raise OfflineError(
            f"network access is disabled in the default test run: refusing to connect to "
            f"{host!r} (only loopback is allowed; this suite must pass offline)"
        )


def _guarded_connect(self: socket.socket, address: Any) -> Any:
    _check(self, address)
    return _REAL_CONNECT(self, address)


def _guarded_connect_ex(self: socket.socket, address: Any) -> Any:
    _check(self, address)
    return _REAL_CONNECT_EX(self, address)


def _guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
    host = address[0]
    if not _is_loopback_host(host):  # before DNS, so it fails fast even with no resolver
        raise OfflineError(
            f"network access is disabled in the default test run: refusing to connect to "
            f"{host!r} (only loopback is allowed; this suite must pass offline)"
        )
    return _REAL_CREATE_CONNECTION(address, *args, **kwargs)


def pytest_configure(config: Any) -> None:
    socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign]
    socket.create_connection = _guarded_create_connection  # type: ignore[assignment]


def pytest_unconfigure(config: Any) -> None:
    socket.socket.connect = _REAL_CONNECT  # type: ignore[method-assign]
    socket.socket.connect_ex = _REAL_CONNECT_EX  # type: ignore[method-assign]
    socket.create_connection = _REAL_CREATE_CONNECTION  # type: ignore[assignment]
