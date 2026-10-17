"""Runtime accessors build each object once, even under concurrent first use (web server)."""

from __future__ import annotations

import threading
import time

from pocketsync.cli import _runtime
from pocketsync.config import Settings


def test_once_builds_a_single_object_under_concurrency():
    rt = _runtime.Runtime(Settings())
    calls = []

    def make():
        calls.append(1)
        time.sleep(0.05)
        return object()

    results = []
    threads = [
        threading.Thread(target=lambda: results.append(rt._once("x", make))) for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1
    assert len({id(r) for r in results}) == 1


def test_once_is_reentrant():
    rt = _runtime.Runtime(Settings())
    assert rt._once("outer", lambda: rt._once("inner", lambda: 41) + 1) == 42


def test_a_slow_build_does_not_block_other_accessors():
    rt = _runtime.Runtime(Settings())
    rt._once("ready", lambda: "cached")
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(5)
        return "slow"

    t = threading.Thread(target=lambda: rt._once("slow", slow))
    t.start()
    started.wait(5)
    t0 = time.monotonic()
    assert rt._once("ready", lambda: "rebuilt") == "cached"
    assert rt._once("other", lambda: "fresh") == "fresh"
    assert time.monotonic() - t0 < 1.0
    release.set()
    t.join()
