"""``pocketsync sync`` — one reconcile pass (or a backfill with ``--since``) through ``land()``.

Dry run by default: Pocket is read (list + get, never the audio URL), nothing is written to
the files root, Mongo or the state store, and ``planned`` lists what ``--apply`` would do.
The state store is wrapped read-only for the dry run, so even an error during the pass is
reported on stdout/stderr instead of being recorded.

With ``--apply``:

1. reconcile from ``watermark - 2 days`` (advances the watermark on success), or with
   ``--since DATE`` backfill from exactly that day (watermark untouched);
2. unless ``--no-enrich``: one enrichment pass over pending docs, then one outbox drain to
   the broker (events-cli). A gateway or broker outage leaves work pending, not failed.

``--no-audio`` skips the audio download (text still lands; a later run with audio fetches
it). Exit 0 on a clean pass, 2 if any page or recording failed (details in ``status``).
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from typing import Any

from pocketsync.cli import _runtime
from pocketsync.cli._commands._common import add_apply, add_json, json_mode, parse_day
from pocketsync.cli._errors import EXIT_ENV_ERROR, CliError
from pocketsync.cli._output import emit_error, emit_result

_STATE_WRITES = frozenset(
    {
        "set_watermark",
        "set_landing_schema",
        "mark_seen",
        "record_run",
        "increment",
        "set_last_webhook_at",
    }
)


class ReadOnlyState:
    """Delegates reads to ``inner``; drops every write. Errors are kept in :attr:`errors`."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.errors_seen: list[dict[str, Any]] = []

    def record_error(self, where: str, message: str, *, when: str | None = None) -> None:
        del when  # StateStore signature; a dry run reports errors without a timestamp
        self.errors_seen.append({"where": where, "message": message})

    def __getattr__(self, name: str) -> Any:
        if name in _STATE_WRITES:
            return lambda *a, **kw: None
        return getattr(self._inner, name)


def cmd_sync(args: argparse.Namespace) -> int:
    since = parse_day(args.since)
    dry_run = not args.apply
    rt = _runtime.get()
    rt.settings.require("api_key", "mongo_uri")
    state = rt.state()
    run_state = ReadOnlyState(state) if dry_run else state
    reconciler = rt.reconciler(state=run_state, dry_run=dry_run)
    reconciler.with_audio = bool(args.audio)

    result = reconciler.backfill(since) if since else reconciler.reconcile()

    enrichment = outbox = None
    if not dry_run and args.enrich:
        enrichment = rt.worker().run_once()
        outbox = asdict(rt.publisher().drain())

    payload = {
        "dry_run": dry_run,
        "mode": "backfill" if since else "reconcile",
        "since": since,
        "audio": args.audio,
        "result": result.stats(),
        "planned": result.planned if dry_run else [],
        "errors": run_state.errors_seen if dry_run else [],
        "enrichment": enrichment,
        "outbox": outbox,
    }
    _emit(payload, json_mode(args))
    if not result.ok:
        emit_error(
            CliError(
                EXIT_ENV_ERROR,
                f"sync pass had {result.errors} error(s)",
                "run 'pocketsync status' for the recorded errors, then retry",
            ),
            json_mode=json_mode(args),
        )
        return EXIT_ENV_ERROR
    return 0


def _emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        emit_result(payload, json_mode=True)
        return
    r = payload["result"]
    head = "dry run (nothing written; pass --apply to commit)" if payload["dry_run"] else "applied"
    lines = [
        f"sync {payload['mode']}: {head}",
        f"listed {r['listed']}, landed {r['landed']}, skipped {r['skipped']}, "
        f"missed-by-webhook {r['missed']}, errors {r['errors']}",
    ]
    lines += [f"  would {p}" for p in payload["planned"]]
    lines += [f"  error: {e['message']}" for e in payload["errors"]]
    if payload["enrichment"] is not None:
        e = payload["enrichment"]
        lines.append(
            f"enrichment: done {e['done']}, unavailable {e['unavailable']}, errors {e['errors']}"
        )
    if payload["outbox"] is not None:
        o = payload["outbox"]
        extra = f" ({o['error']})" if o.get("error") else ""
        lines.append(f"outbox: published {o['published']}, pending {o['pending']}{extra}")
    emit_result("\n".join(lines), json_mode=False)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "sync",
        help="One reconcile pass (backfill with --since). Dry run unless --apply.",
    )
    p.add_argument(
        "--since", metavar="DATE", help="Backfill from this day (YYYY-MM-DD) instead of reconcile."
    )
    p.add_argument(
        "--with-audio",
        dest="audio",
        action="store_true",
        default=True,
        help="Download audio files (default).",
    )
    p.add_argument("--no-audio", dest="audio", action="store_false", help="Skip audio.")
    p.add_argument(
        "--enrich",
        dest="enrich",
        action="store_true",
        default=True,
        help="After --apply, run one enrichment pass and drain the outbox (default).",
    )
    p.add_argument(
        "--no-enrich", dest="enrich", action="store_false", help="Land only; skip enrichment."
    )
    add_apply(p, "the landing (files + Mongo + state)")
    add_json(p)
    p.set_defaults(func=cmd_sync)
