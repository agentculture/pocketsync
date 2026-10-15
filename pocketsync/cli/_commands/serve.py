"""``pocketsync serve`` — the long-running daemon: receiver + landing + reconciler + enrichment.

Requires ``POCKETSYNC_API_KEY``, ``POCKETSYNC_MONGO_URI`` and ``POCKETSYNC_WEBHOOK_SECRET``.
At start it creates the regular Mongo indexes and the vector indexes
(:func:`pocketsync.vindex.ensure_index`); a vector-space mismatch is fatal (exit 1, run
``pocketsync reembed``), while a server without Atlas Search is only a warning. It then runs
:class:`pocketsync.serve.Service` -- loopback webhook receiver on ``--port`` (default
``webhook_port``, 8787), landing consumer, reconciler loop, enrichment worker and outbox
drain -- until SIGTERM/SIGINT, stops the receiver, joins the threads and exits 0.
"""

from __future__ import annotations

import argparse

from pocketsync import vindex
from pocketsync.cli import _runtime
from pocketsync.cli._commands._common import add_json, json_mode
from pocketsync.cli._errors import EXIT_USER_ERROR, CliError
from pocketsync.cli._output import emit_diagnostic, emit_result
from pocketsync.cli._signals import install_signal_handlers, run_until_signal
from pocketsync.enrich.gateway import EMBED_DIMS, EMBED_MODEL
from pocketsync.serve import Service, ServiceTiming


def prepare_store(rt: _runtime.Runtime) -> None:
    """Idempotent index setup. Raises CliError on a vector-space mismatch."""
    store = rt.store_sink()
    store.ensure_indexes()
    ensure_state = getattr(rt.state(), "ensure_indexes", None)
    if ensure_state is not None:
        ensure_state()
    try:
        vindex.ensure_index(store.collection, EMBED_MODEL, EMBED_DIMS)
    except CliError:
        raise
    except Exception as exc:  # e.g. a plain mongod without Atlas Search
        emit_diagnostic(
            f"warning: vector indexes not ensured ({type(exc).__name__}: {exc}); "
            "vector search will be unavailable until they exist"
        )


def cmd_serve(args: argparse.Namespace) -> int:
    rt = _runtime.get()
    s = rt.settings
    s.require("api_key", "mongo_uri", "webhook_secret")
    port = s.webhook_port if args.port is None else args.port
    if not 0 <= port <= 65535:
        raise CliError(EXIT_USER_ERROR, f"invalid --port {port}", "Pass a port in 0..65535.")
    prepare_store(rt)
    service = Service(
        rt.source(),
        files_sink=rt.files_sink(),
        store_sink=rt.store_sink(),
        state=rt.state(),
        secret=s.webhook_secret or "",
        port=port,
        reconciler=rt.reconciler(),
        worker=rt.worker(),
        publisher=rt.publisher(),
        timing=ServiceTiming(reconcile_interval=s.reconcile_interval),
    )

    def started() -> None:
        emit_diagnostic(
            f"serving: webhook receiver on http://127.0.0.1:{service.port}/hooks/pocket, "
            f"reconcile every {s.reconcile_interval}s (SIGTERM or Ctrl-C to stop)"
        )

    leftover = run_until_signal(
        service, service.stop_event, on_started=started, install=install_signal_handlers
    )
    if leftover:
        emit_diagnostic(f"warning: threads still running at exit: {', '.join(leftover)}")
    result = {
        "stopped": True,
        "port": port,
        "leftover_threads": leftover,
        "stats": dict(service.stats),
    }
    if json_mode(args):
        emit_result(result, json_mode=True)
    else:
        st = result["stats"]
        emit_result(
            f"serve stopped: landed {st['landed']}, deleted {st['deleted']}, "
            f"merged {st['merged']}, errors {st['errors']}",
            json_mode=False,
        )
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "serve",
        help="Run receiver + landing + reconciler + enrichment in one process (SIGTERM stops).",
    )
    p.add_argument(
        "--port",
        type=int,
        default=None,
        metavar="P",
        help="Loopback port for the webhook receiver (default: webhook_port, 8787).",
    )
    add_json(p)
    p.set_defaults(func=cmd_serve)
