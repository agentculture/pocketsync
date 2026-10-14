"""``pocketsync list`` — remote Pocket recordings with their local sync status.

Read-only. One ``list_recordings`` page (``--limit``, at most 100) from ``--since`` (a day,
inclusive) to today. For each recording it reports whether the Mongo store has it
(``synced``), whether the stored copy matches Pocket's ``updated_at`` (``up_to_date``, the
same no-extra-API-call test the reconciler uses), the stored ``content_hash`` and the
enrichment status. Without ``POCKETSYNC_MONGO_URI`` the status fields are ``null`` and a
diagnostic says why.
"""

from __future__ import annotations

import argparse
from typing import Any

from pocketsync.cli import _runtime
from pocketsync.cli._commands._common import add_json, json_mode, parse_day
from pocketsync.cli._errors import EXIT_USER_ERROR, CliError
from pocketsync.cli._output import emit_diagnostic, emit_result
from pocketsync.pocket.base import MAX_PAGE_LIMIT

DEFAULT_LIMIT = 20


def _status(collection: Any, item: Any) -> dict[str, Any]:
    if collection is None:
        return {"synced": None, "up_to_date": None, "content_hash": None, "enrichment": None}
    d = collection.find_one(
        {"_id": item.id}, {"source": 1, "content_hash": 1, "enrichment.status": 1}
    )
    if d is None:
        return {"synced": False, "up_to_date": False, "content_hash": None, "enrichment": None}
    stored = (d.get("source") or {}).get("updated_at")
    return {
        "synced": True,
        "up_to_date": stored == item.updated_at,
        "content_hash": d.get("content_hash"),
        "enrichment": (d.get("enrichment") or {}).get("status"),
    }


def _label(row: dict[str, Any]) -> str:
    if row["synced"] is None:
        return "status unknown"
    if not row["synced"]:
        return "not synced"
    state = "up to date" if row["up_to_date"] else "stale"
    return f"synced, {state}, enrichment {row['enrichment'] or '-'}"


def cmd_list(args: argparse.Namespace) -> int:
    if not 1 <= args.limit <= MAX_PAGE_LIMIT:
        raise CliError(
            EXIT_USER_ERROR,
            f"--limit must be between 1 and {MAX_PAGE_LIMIT}",
            f"Pass --limit 1..{MAX_PAGE_LIMIT}.",
        )
    since = parse_day(args.since)
    rt = _runtime.get()
    source = rt.source()
    collection = None
    if rt.settings.mongo_uri:
        collection = rt.store_sink().collection
    else:
        emit_diagnostic("note: POCKETSYNC_MONGO_URI is not set; sync status is unknown")
    page = source.list_recordings(start_date=since, page=1, limit=args.limit)
    rows = []
    for item in page.recordings:
        rows.append(
            {
                "id": item.id,
                "title": item.title,
                "recording_at": item.recording_at,
                "updated_at": item.updated_at,
                **_status(collection, item),
            }
        )
    result = {"since": since, "total": page.total, "has_more": page.has_more, "recordings": rows}
    if json_mode(args):
        emit_result(result, json_mode=True)
        return 0
    if not rows:
        emit_result("no recordings", json_mode=False)
        return 0
    lines = [f"{r['id']}  {r['recording_at'] or '-'}  {r['title']}  [{_label(r)}]" for r in rows]
    if page.has_more:
        lines.append(f"... more available (total {page.total}); raise --limit or narrow --since")
    emit_result("\n".join(lines), json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "list",
        help="List remote Pocket recordings with local sync status. Read-only.",
    )
    p.add_argument(
        "--since", metavar="DATE", help="Only recordings on/after this day (YYYY-MM-DD)."
    )
    p.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"How many to show (1-{MAX_PAGE_LIMIT}, default {DEFAULT_LIMIT}).",
    )
    add_json(p)
    p.set_defaults(func=cmd_list)
