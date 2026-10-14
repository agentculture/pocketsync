"""SIGTERM/SIGINT wiring for the long-running verbs (``serve``, ``watch``).

Handlers only set a shared :class:`threading.Event`; the loops watch it and wind down. That
keeps shutdown graceful (no exception raised mid-write) and works the same for systemd's
SIGTERM and an operator's Ctrl-C.
"""

from __future__ import annotations

import signal
import threading
from typing import Any, Callable

_SIGNALS = (signal.SIGTERM, signal.SIGINT)
_WAIT_SLICE = 0.5


def install_signal_handlers(stop: threading.Event) -> Callable[[], None]:
    """Make SIGTERM/SIGINT set ``stop``; return a function restoring the old handlers.

    Outside the main thread (where ``signal.signal`` is not allowed) this is a no-op.
    """
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    previous: dict[int, Any] = {}

    def _handler(signum: int, frame: Any) -> None:
        stop.set()

    for sig in _SIGNALS:
        previous[sig] = signal.signal(sig, _handler)

    def restore() -> None:
        for sig, old in previous.items():
            signal.signal(sig, old)

    return restore


def wait(stop: threading.Event) -> None:
    """Block until ``stop`` is set, in short slices so signal handlers run promptly."""
    while not stop.wait(_WAIT_SLICE):
        pass


def run_until_signal(
    service: Any,
    stop: threading.Event,
    *,
    on_started: Callable[[], None] | None = None,
    install: Callable[[threading.Event], Callable[[], None]] = install_signal_handlers,
) -> list[str]:
    """Start ``service``, wait for a signal (or ``stop``), stop it; return leftover threads."""
    restore = install(stop)
    try:
        service.start()
        if on_started is not None:
            on_started()
        wait(stop)
    finally:
        leftover = service.stop()
        restore()
    return leftover
