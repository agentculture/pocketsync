"""Re-enrichment selection shared by the CLI, MCP and web surfaces.

``plan_enrich`` selects recordings, reports what a re-enrich would cost, and (only with
``apply=True``) re-queues them through :meth:`MongoSink.requeue`. It returns plain dicts.
"""

from __future__ import annotations

import re
from typing import Any

from pocketsync import vindex
from pocketsync.cli._errors import EXIT_USER_ERROR, CliError
from pocketsync.sinks.mongo import (
    ENRICHMENT_DONE,
    ENRICHMENT_ERROR,
    ENRICHMENT_PENDING,
    MODE_CLASSIFY,
    MODE_EMBED,
    MODE_FULL,
    PATH_ENRICHMENT_STATUS,
)

STATUSES = (ENRICHMENT_PENDING, ENRICHMENT_DONE, ENRICHMENT_ERROR)
ONLY_MODES = {None: MODE_FULL, "all": MODE_FULL, "classify": MODE_CLASSIFY, "embed": MODE_EMBED}
_CALLS_PER_DOC = {MODE_FULL: 2, MODE_CLASSIFY: 1, MODE_EMBED: 1}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.+\-Zz]+$")
_PATH_LABELS = "enrichment.labels"


def _err(message: str, remediation: str) -> CliError:
    return CliError(EXIT_USER_ERROR, message, remediation)


def _check_date(name: str, value: str) -> str:
    if not (_DATE.match(value) or _ISO.match(value)):
        raise _err(
            f"invalid --{name} {value!r}",
            f"use YYYY-MM-DD or a full ISO timestamp, e.g. --{name} 2026-05-01",
        )
    return value


def plan_enrich(
    store: Any,
    *,
    all: bool = False,  # noqa: A002 - mirrors the --all flag
    since: str | None = None,
    until: str | None = None,
    ids: list[str] | None = None,
    label: str | None = None,
    status: str | None = None,
    only: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Select recordings to re-enrich; write nothing unless ``apply`` is true.

    Selectors (at least one required; they combine with AND): ``all``, ``since`` /
    ``until`` (on ``recording_at``; a date-only value is compared by its date prefix,
    ``until`` inclusive of that whole day), ``ids``, ``label`` (in ``enrichment.labels``)
    and ``status`` (pending, done or error). Docs deleted in Pocket are never selected.

    ``only`` picks the mode: None or "all" -> full, "classify", "embed".

    Estimated gateway calls = count * calls per doc: classify-only 1, embed-only 1,
    full 2 (one classify plus one embed batch call per doc).

    Returns ``{dry_run, ids, count, mode, estimated_gateway_calls, updated}``.
    Raises CliError (exit 1) for no selector, bad status/only/date.
    """
    if only not in ONLY_MODES:
        raise _err(
            f"unknown --only value {only!r}", "use one of: all, classify, embed (or omit it)"
        )
    if status is not None and status not in STATUSES:
        raise _err(f"unknown --status value {status!r}", f"use one of: {', '.join(STATUSES)}")
    if not (all or since or until or ids or label or status):
        raise _err(
            "no selector given; refusing to re-enrich nothing or everything by accident",
            "pass --all, or one of --since, --until, --id, --label, --status",
        )
    mode = ONLY_MODES[only]

    query: dict[str, Any] = {vindex.PATH_DELETED: {"$ne": True}}
    rng: dict[str, str] = {}
    if since:
        rng["$gte"] = _check_date("since", since)
    if until:
        end = _check_date("until", until)
        rng["$lte"] = end + "\uffff" if _DATE.match(end) else end
    if rng:
        query[vindex.PATH_RECORDING_AT] = rng
    if ids:
        query["_id"] = {"$in": list(ids)}
    if label:
        query[_PATH_LABELS] = label
    if status:
        query[PATH_ENRICHMENT_STATUS] = status

    selected = sorted(d["_id"] for d in store.collection.find(query, {"_id": 1}))
    updated = store.requeue(selected, mode) if apply and selected else 0
    return {
        "dry_run": not apply,
        "ids": selected,
        "count": len(selected),
        "mode": mode,
        "estimated_gateway_calls": len(selected) * _CALLS_PER_DOC[mode],
        "updated": updated,
    }
