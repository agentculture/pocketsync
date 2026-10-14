"""``pocketsync reembed`` — find recordings whose vectors use another model/dimension.

Dry run by default: lists the ids :func:`pocketsync.vindex.reembed_plan` reports (stored
``enrichment.model`` / ``enrichment.dims`` differ from the configured embedding model).
With ``--apply`` it sets exactly those docs' ``enrichment.status`` to ``pending`` -- no other
field and no other document is written -- and the enrichment worker (``serve`` or
``sync --apply``) re-embeds them, overwriting model, dims and vectors.
"""

from __future__ import annotations

import argparse

from pocketsync import vindex
from pocketsync.cli import _runtime
from pocketsync.cli._commands._common import add_apply, add_json, json_mode
from pocketsync.cli._output import emit_result
from pocketsync.enrich.gateway import EMBED_DIMS, EMBED_MODEL
from pocketsync.sinks.mongo import ENRICHMENT_PENDING, PATH_ENRICHMENT_STATUS


def cmd_reembed(args: argparse.Namespace) -> int:
    rt = _runtime.get()
    rt.settings.require("mongo_uri")
    collection = rt.store_sink().collection
    ids = vindex.reembed_plan(collection, EMBED_MODEL, EMBED_DIMS)
    updated = 0
    if args.apply and ids:
        res = collection.update_many(
            {"_id": {"$in": ids}}, {"$set": {PATH_ENRICHMENT_STATUS: ENRICHMENT_PENDING}}
        )
        updated = int(res.modified_count)
    result = {
        "dry_run": not args.apply,
        "model": EMBED_MODEL,
        "dims": EMBED_DIMS,
        "ids": ids,
        "count": len(ids),
        "updated": updated,
    }
    if json_mode(args):
        emit_result(result, json_mode=True)
        return 0
    target = f"{EMBED_MODEL}/{EMBED_DIMS}"
    if not ids:
        emit_result(f"nothing to re-embed: every stored vector uses {target}", json_mode=False)
        return 0
    lines = [f"{len(ids)} recording(s) use another model/dimension than {target}:"]
    lines += [f"  {i}" for i in ids]
    if args.apply:
        lines.append(f"marked {updated} pending; the enrichment worker will re-embed them")
    else:
        lines.append("dry run (nothing written); pass --apply to mark them pending")
    emit_result("\n".join(lines), json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "reembed",
        help="List recordings embedded with another model; --apply queues them for re-embedding.",
    )
    add_apply(p, "the re-embed (sets enrichment.status=pending on affected docs)")
    add_json(p)
    p.set_defaults(func=cmd_reembed)
