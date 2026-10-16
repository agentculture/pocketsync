"""``pocketsync web`` — the private chat site over the verb registry (loopback only).

The server lives in :mod:`pocketsync.web.server` and is imported lazily. If this build does
not ship it, the verb fails with exit 2 and a hint. ``--port`` defaults to the ``web_port``
setting (``POCKETSYNC_WEB_PORT``, 8788). ``--no-access-check`` turns off the Cloudflare
Access JWT check, for loopback development only; the server decides whether it may start
without one.

Contract for the server module: ``serve(runtime, *, port, access_check)`` runs until
SIGTERM/SIGINT and returns None or an exit code.
"""

from __future__ import annotations

import argparse
import importlib

from pocketsync.cli import _runtime
from pocketsync.cli._commands._common import add_json
from pocketsync.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError

SERVER_MODULE = "pocketsync.web.server"
MISSING_HINT = (
    "this pocketsync build has no web server (pocketsync.web.server); "
    "install a pocketsync release that ships `pocketsync web`"
)


def cmd_web(args: argparse.Namespace) -> int | None:
    try:
        server = importlib.import_module(SERVER_MODULE)
    except ImportError as exc:
        raise CliError(EXIT_ENV_ERROR, f"web server unavailable: {exc}", MISSING_HINT) from exc
    rt = _runtime.get()
    port = rt.settings.web_port if args.port is None else args.port
    if not 0 <= port <= 65535:
        raise CliError(EXIT_USER_ERROR, f"invalid --port {port}", "Pass a port in 0..65535.")
    rc = server.serve(rt, port=port, access_check=not args.no_access_check)
    return rc if isinstance(rc, int) else None


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "web",
        help="Run the private chat site (loopback; Cloudflare Access JWT checked per request).",
    )
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help="Loopback port (default: POCKETSYNC_WEB_PORT, 8788).",
    )
    p.add_argument(
        "--no-access-check",
        action="store_true",
        help="Skip the Cloudflare Access JWT check (loopback development only).",
    )
    add_json(p)
    p.set_defaults(func=cmd_web)
