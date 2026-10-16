"""``pocketsync status`` — sync and webhook-path health from the state store. Read-only.

Reports the reconcile watermark, the last run (start, finish, stats), the last *verified*
webhook time, ``webhook_missed`` (recordings the reconciler found that no webhook delivered:
a growing count means the push path is broken -- tunnel down, Access bypass gone, secret
rotated), the enrichment backlog (pending docs not deleted in Pocket) and the most recent
errors. Because the reconciler bumps ``webhook_missed`` on every pass, a broken webhook path
shows up here within one reconcile interval.
"""

from __future__ import annotations

import argparse
from typing import Any

from pocketsync.cli import _runtime
from pocketsync.cli._commands._common import add_json, json_mode, kv_lines
from pocketsync.cli._output import emit_result
from pocketsync.sinks.mongo import ENRICHMENT_PENDING, PATH_ENRICHMENT_STATUS
from pocketsync.state import COUNTER_WEBHOOK_MISSED
from pocketsync.vindex import PATH_DELETED

LAST_ERRORS = 10


def enrichment_backlog(collection: Any) -> int:
    return int(
        collection.count_documents(
            {PATH_ENRICHMENT_STATUS: ENRICHMENT_PENDING, PATH_DELETED: {"$ne": True}}
        )
    )


def enrichment_errors(collection: Any) -> tuple[int, int]:
    """``(retrying, failed)``: ``error`` docs still under the attempt cap, and those past it."""
    from pocketsync.sinks.mongo import (
        ENRICHMENT_ERROR,
        MAX_ENRICHMENT_ATTEMPTS,
        PATH_ENRICHMENT_ATTEMPTS,
    )

    base = {PATH_ENRICHMENT_STATUS: ENRICHMENT_ERROR, PATH_DELETED: {"$ne": True}}
    cap = {PATH_ENRICHMENT_ATTEMPTS: {"$gte": MAX_ENRICHMENT_ATTEMPTS}}
    failed = int(collection.count_documents({**base, **cap}))
    return int(collection.count_documents(base)) - failed, failed


def snapshot(rt: Any) -> dict[str, Any]:
    rt.settings.require("mongo_uri")
    state = rt.state()
    errors = state.errors()
    collection = rt.store_sink().collection
    retrying, failed = enrichment_errors(collection)
    return {
        "watermark": state.get_watermark(),
        "last_run": state.last_run(),
        "last_webhook_at": state.get_last_webhook_at(),
        "webhook_missed": int(state.counters().get(COUNTER_WEBHOOK_MISSED, 0)),
        "enrichment_backlog": enrichment_backlog(collection),
        "enrichment_retrying": retrying,
        "enrichment_failed": failed,
        "error_count": len(errors),
        "last_errors": errors[-LAST_ERRORS:],
    }


def cmd_status(args: argparse.Namespace) -> None:
    snap = snapshot(_runtime.get())
    if json_mode(args):
        emit_result(snap, json_mode=True)
        return
    run = snap["last_run"] or {}
    stats = run.get("stats") or {}
    run_text = (
        f"{run.get('finished')} (landed {stats.get('landed', 0)}, errors {stats.get('errors', 0)})"
        if run
        else None
    )
    text = kv_lines(
        [
            ("watermark", snap["watermark"]),
            ("last run", run_text),
            ("last webhook", snap["last_webhook_at"]),
            ("webhook_missed", snap["webhook_missed"]),
            ("enrichment backlog", snap["enrichment_backlog"]),
            ("enrichment retrying", snap["enrichment_retrying"]),
            ("enrichment failed", snap["enrichment_failed"]),
            ("errors kept", snap["error_count"]),
        ]
    )
    for e in snap["last_errors"]:
        text += f"\n  {e.get('at', '-')} [{e.get('where', '-')}] {e.get('message', '')}"
    emit_result(text, json_mode=False)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "status",
        help="Watermark, last run, webhook health, enrichment backlog, last errors. Read-only.",
    )
    add_json(p)
    p.set_defaults(func=cmd_status)
