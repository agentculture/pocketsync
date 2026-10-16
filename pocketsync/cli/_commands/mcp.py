"""``pocketsync mcp`` — serve the verb registry as MCP tools over stdio.

The server lives in :mod:`pocketsync.mcp.server` behind the ``pocketsync[mcp]`` extra; it is
imported lazily here, so the core CLI never imports the mcp SDK. When the server (or the SDK)
is not installed the verb fails with exit 2 and an install hint, and writes nothing to
stdout: stdout is reserved for the MCP protocol. ``--json`` only changes how such an error is
rendered (on stderr).

Contract for the server module: ``run_stdio()`` runs until the client disconnects and returns
None or an exit code.
"""

from __future__ import annotations

import argparse
import importlib

from pocketsync.cli._commands._common import add_json
from pocketsync.cli._errors import EXIT_ENV_ERROR, CliError

SERVER_MODULE = "pocketsync.mcp.server"
INSTALL_HINT = "pip install 'pocketsync[mcp]' (the MCP server needs the mcp extra)"


def cmd_mcp(args: argparse.Namespace) -> int | None:
    try:
        server = importlib.import_module(SERVER_MODULE)
        rc = server.run_stdio()  # raises ServerExtraMissing (an ImportError) without the SDK
    except ImportError as exc:
        raise CliError(EXIT_ENV_ERROR, f"MCP server unavailable: {exc}", INSTALL_HINT) from exc
    return rc if isinstance(rc, int) else None


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "mcp",
        help="Serve search/get/transcript/recordings/actions/enrich as MCP tools over stdio.",
    )
    add_json(p)
    p.set_defaults(func=cmd_mcp)
