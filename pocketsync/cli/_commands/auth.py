"""``pocketsync auth`` — verify the Pocket API key with a single list call (limit 1).

Read-only. Requires ``POCKETSYNC_API_KEY``. The key is passed to the Pocket client and
nowhere else: it is never printed, in text or JSON mode, on success or on failure.
"""

from __future__ import annotations

import argparse

from pocketsync.cli import _runtime
from pocketsync.cli._commands._common import add_json, json_mode
from pocketsync.cli._output import emit_result


def cmd_auth(args: argparse.Namespace) -> int:
    rt = _runtime.get()
    rt.settings.require("api_key")
    page = rt.source().list_recordings(page=1, limit=1)
    result = {
        "ok": True,
        "total": page.total,
        "sample": len(page.recordings),
    }
    if json_mode(args):
        emit_result(result, json_mode=True)
    else:
        total = "unknown" if page.total is None else page.total
        emit_result(f"ok: Pocket API key accepted ({total} recording(s) visible)", json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "auth",
        help="Verify the Pocket API key (one list call, limit 1). Read-only.",
    )
    add_json(p)
    p.set_defaults(func=cmd_auth)
