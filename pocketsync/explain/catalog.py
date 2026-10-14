"""Markdown catalog for ``pocketsync explain <path>``.

Each entry is verbatim markdown. Keys are command-path tuples. The empty tuple
and ``("pocketsync",)`` both resolve to the root entry.

Keep bodies self-contained: an agent reading one entry should get enough
context without chaining reads.
"""

from __future__ import annotations

_ROOT = """\
# pocketsync

Sync bridge for the Pocket AI voice recorder. It pulls recordings, transcripts
(STT), summaries and action items out of the Pocket cloud into a landing zone we
own: one MongoDB document per recording (enriched with embeddings and labels for
vector search), a local directory tree, and an append-only `events.jsonl`
outbox that culture-rules workflows trigger on. It does not route data further.

## Operational verbs

- `pocketsync auth` — verify the Pocket API key (one list call).
- `pocketsync list` — remote recordings with local sync status.
- `pocketsync sync [--apply]` — one reconcile pass, or a backfill with `--since`.
- `pocketsync watch` — reconciler-only loop.
- `pocketsync serve` — webhook receiver + landing + reconciler + enrichment.
- `pocketsync status` — watermark, last run, webhook health, backlog, errors.
- `pocketsync reembed [--apply]` — queue recordings embedded with another model.

Write verbs (`sync`, `reembed`) are dry runs unless `--apply`.

## Introspection verbs

- `pocketsync whoami` — identity probe from `culture.yaml`.
- `pocketsync learn` — structured self-teaching prompt.
- `pocketsync explain <path>` — markdown docs for any noun/verb.
- `pocketsync overview` — descriptive snapshot of the agent.
- `pocketsync doctor` — identity invariants plus runtime health checks.
- `pocketsync cli overview` — describe the CLI surface.

## Exit-code policy

- `0` success
- `1` user-input error
- `2` environment / setup error (missing setting, backend unreachable)
- `3+` reserved

## See also

- `pocketsync explain sync`
- `pocketsync explain serve`
- `pocketsync explain doctor`
"""

_WHOAMI = """\
# pocketsync whoami

Reports the agent's identity from `culture.yaml`: nick (`suffix`), backend,
served model, and the package version. Read-only.

## Usage

    pocketsync whoami
    pocketsync whoami --json
"""

_LEARN = """\
# pocketsync learn

Prints a structured self-teaching prompt covering purpose, command map,
exit-code policy, `--json` support, and the `explain` pointer.

## Usage

    pocketsync learn
    pocketsync learn --json
"""

_EXPLAIN = """\
# pocketsync explain <path>

Prints markdown documentation for any noun/verb path. Unlike `--help` (terse,
positional), `explain` is global and addressable by path.

## Usage

    pocketsync explain pocketsync
    pocketsync explain whoami
    pocketsync explain --json <path>
"""

_OVERVIEW = """\
# pocketsync overview

Read-only descriptive snapshot of the agent: identity (from `culture.yaml`), the
verb surface, and the sibling-pattern artifacts the agent carries. Accepts an
ignored `target` so a stray path never hard-fails.

## Usage

    pocketsync overview
    pocketsync overview --json
"""

_DOCTOR = """\
# pocketsync doctor

Checks the agent-identity invariants `steward doctor` verifies:
prompt-file-present and backend-consistency (`claude` → `CLAUDE.md`), plus a
skills-present check, then the runtime health of the configured deployment.
Every check carries `status` (`ok`, `warn`, `fail`, `info`) and a remediation.
Only `fail` makes doctor unhealthy (exit 1); warnings exit 0.

prompt-file-present requires the *resident* prompt the declared backend
actually reads. Other harness prompt files recognized under the same backend
name (`AGENTS.override.md`, `.pi/SYSTEM.md`, `QWEN.md`) belong to
interactively available harnesses the mesh daemon never loads; they are
reported by the informational harness-prompts check and never substituted.

## Runtime checks

- `mongo_auth` — `ping` with `POCKETSYNC_MONGO_URI` (credentials included).
  The URI is never echoed. On failure the Mongo-dependent checks are skipped.
- `gateway_reachability` — `GET <gateway root>/capabilities` (one level above
  the `/v1` in `POCKETSYNC_GATEWAY_URL`; unauthenticated, no model call).
  Unreachable fails; `gemma2-embed` not advertised or
  `POCKETSYNC_GATEWAY_API_KEY` unset warns.
- `vector_index_model` — the vector index and stored vectors match
  gemma2-embed/768; a mismatch fails (run `pocketsync reembed --apply`).
- `webhook_freshness` — with `POCKETSYNC_WEBHOOK_SECRET` set (serve
  expected), warns if no verified webhook arrived in 24 hours.
- `webhook_missed`, `enrichment_backlog`, `recent_errors` — the `status`
  numbers; warn when non-zero, above 100, or newer than a day.

With nothing configured, a single `runtime_config` info check replaces the
runtime checks and nothing is probed (a bare wheel install exits 0).

## Usage

    pocketsync doctor
    pocketsync doctor --json
"""

_CLI = """\
# pocketsync cli

Noun group for CLI-surface introspection. `cli overview` describes the CLI
itself (distinct from the global `overview`, which describes the agent).

## Usage

    pocketsync cli overview
    pocketsync cli overview --json
"""


_AUTH = """\
# pocketsync auth

Verifies the Pocket API key (`POCKETSYNC_API_KEY`, a `pk_...` key) with a
single `list recordings` call, limit 1. Read-only. Reports `ok` and how many
recordings the key can see. The key itself is never printed.

Exit 2 if the key is unset or Pocket rejects it (401) or rate-limits (429).

## Usage

    pocketsync auth
    pocketsync auth --json
"""

_LIST = """\
# pocketsync list

Lists remote Pocket recordings (one page) with their local sync status.
Read-only.

For each recording: `synced` (the Mongo store has a document), `up_to_date`
(the stored copy has Pocket's current `updated_at`; the same test the
reconciler uses, no extra API calls), the stored `content_hash` and the
`enrichment` status (`pending`, `done`, `error`). Without
`POCKETSYNC_MONGO_URI` the status fields are `null`.

## Usage

    pocketsync list [--since YYYY-MM-DD] [--limit N]   # N in 1..100, default 20
    pocketsync list --json
"""

_SYNC = """\
# pocketsync sync

One pass through the same landing path the webhook uses. **Dry run unless
`--apply`**: a dry run reads Pocket (never requests audio URLs) and writes
nothing to the files root, MongoDB or the state store; `planned` lists the
writes `--apply` would make.

- Without `--since`: reconcile from `watermark - 2 days` to today; the
  watermark advances only when the whole pass succeeded. New recordings older
  than 10 minutes that no webhook delivered bump `webhook_missed`.
- With `--since DATE`: backfill from exactly that day; watermark untouched.
- `--with-audio` (default) / `--no-audio`: download audio or text only.
- After `--apply`, unless `--no-enrich`: one enrichment pass over pending
  documents, then one outbox drain to the broker (events-cli). A gateway or
  broker outage leaves the work pending.

Exit 0 on a clean pass, 2 if any page or recording failed (see
`pocketsync status`).

## Usage

    pocketsync sync                       # dry run
    pocketsync sync --apply
    pocketsync sync --since 2026-09-01 --apply --no-audio
    pocketsync sync --apply --json
"""

_WATCH = """\
# pocketsync watch

The reconciler-only loop, in the foreground: one reconcile pass every
`--interval` seconds (default `POCKETSYNC_RECONCILE_INTERVAL`, 300) until
SIGTERM or Ctrl-C; the pass in flight finishes first. No webhook receiver and
no enrichment (that is `serve`). Errors are recorded in the state store and
the loop keeps going.

## Usage

    pocketsync watch
    pocketsync watch --interval 60 --json
"""

_SERVE = """\
# pocketsync serve

The long-running daemon (for example a systemd user unit). One process runs:

- the webhook receiver on `127.0.0.1:<port>/hooks/pocket` (loopback only;
  HMAC-verified, deduped, acked fast; `--port`, default
  `POCKETSYNC_WEBHOOK_PORT`, 8787);
- a landing consumer: `recording.deleted` tombstones, `recording.merged`
  marks `merged_into` and re-lands the survivor, anything else lands the
  recording;
- the reconciler loop (every `POCKETSYNC_RECONCILE_INTERVAL` seconds);
- the enrichment worker and a periodic outbox drain.

At start it creates the Mongo indexes and the vector indexes. A vector-space
mismatch is fatal (exit 1; run `pocketsync reembed`). SIGTERM or SIGINT stops
the receiver, lets queued events land, joins the threads and exits 0.

Requires `POCKETSYNC_API_KEY`, `POCKETSYNC_MONGO_URI` and
`POCKETSYNC_WEBHOOK_SECRET`.

## Usage

    pocketsync serve
    pocketsync serve --port 8787 --json
"""

_STATUS = """\
# pocketsync status

Read-only health of the sync and webhook paths, from the state store:

- `watermark` — the reconcile cursor;
- `last_run` — start, finish and stats of the last reconcile/backfill pass;
- `last_webhook_at` — time of the last verified webhook;
- `webhook_missed` — recordings the reconciler found that no webhook delivered
  (cumulative). A rising count means the push path is broken: tunnel down,
  Access bypass gone, or the secret rotated. It moves within one reconcile
  interval;
- `enrichment_backlog` — documents pending enrichment;
- `last_errors` — the most recent recorded errors (`error_count` kept).

## Usage

    pocketsync status
    pocketsync status --json
"""

_REEMBED = """\
# pocketsync reembed

Finds recordings whose stored vectors use another embedding model or dimension
than the configured one (gemma2-embed, 768). **Dry run unless `--apply`**: it
only lists their ids. With `--apply` it sets exactly those documents'
`enrichment.status` to `pending`, and writes nothing else. The enrichment worker
(`serve`, or `sync --apply`) then re-embeds them.

## Usage

    pocketsync reembed
    pocketsync reembed --apply --json
"""


ENTRIES: dict[tuple[str, ...], str] = {
    (): _ROOT,
    ("pocketsync",): _ROOT,
    ("whoami",): _WHOAMI,
    ("learn",): _LEARN,
    ("explain",): _EXPLAIN,
    ("overview",): _OVERVIEW,
    ("doctor",): _DOCTOR,
    ("cli",): _CLI,
    ("cli", "overview"): _CLI,
    ("auth",): _AUTH,
    ("list",): _LIST,
    ("sync",): _SYNC,
    ("watch",): _WATCH,
    ("serve",): _SERVE,
    ("status",): _STATUS,
    ("reembed",): _REEMBED,
}
