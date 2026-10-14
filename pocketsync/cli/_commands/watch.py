"""``pocketsync watch`` — the reconciler-only loop, in the foreground.

Requires ``POCKETSYNC_API_KEY`` and ``POCKETSYNC_MONGO_URI``. Runs one reconcile pass every
``--interval`` seconds (default: ``reconcile_interval``, 300) through the same ``land()`` the
webhook path uses, until SIGTERM or Ctrl-C; the pass in flight finishes first. No webhook
receiver and no enrichment -- that is ``serve``. Errors are recorded in the state store and
the loop keeps going.
"""

from __future__ import annotations

import argparse
import threading

from pocketsync.cli import _runtime
from pocketsync.cli._commands._common import add_json, json_mode, positive
from pocketsync.cli._output import emit_diagnostic, emit_result
from pocketsync.cli._signals import install_signal_handlers


def cmd_watch(args: argparse.Namespace) -> int:
    rt = _runtime.get()
    interval = positive(
        args.interval if args.interval is not None else rt.settings.reconcile_interval,
        "--interval",
    )
    rt.settings.require("api_key", "mongo_uri")
    reconciler = rt.reconciler()
    stop = threading.Event()
    restore = install_signal_handlers(stop)
    emit_diagnostic(f"watching: reconcile every {interval:g}s (Ctrl-C or SIGTERM to stop)")
    try:
        reconciler.run_forever(interval, stop)
    finally:
        restore()
    result = {"stopped": True, "interval": interval, "last_run": rt.state().last_run()}
    if json_mode(args):
        emit_result(result, json_mode=True)
    else:
        last = (result["last_run"] or {}).get("finished")
        emit_result(f"watch stopped (last run finished {last or '-'})", json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "watch",
        help="Reconciler-only loop in the foreground (Ctrl-C/SIGTERM stops).",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=None,
        metavar="S",
        help="Seconds between passes (default: reconcile_interval, 300).",
    )
    add_json(p)
    p.set_defaults(func=cmd_watch)
