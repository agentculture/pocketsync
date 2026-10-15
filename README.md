# pocketsync

Sync Pocket voice-recorder recordings, transcripts (STT) and summaries from the
Pocket cloud into MongoDB (with vector search) and optional local files - a
durable landing zone that culture-rules workflows fan out to anywhere.

## What it does

[Pocket](https://heypocket.com) records audio and produces transcripts,
summaries and action items in its own cloud. pocketsync copies that data into
a store you own:

- **MongoDB** (one document per recording, with enrichment and vectors), and
- **local disk** (an optional directory tree plus the `events.jsonl` outbox).

That is the whole job. pocketsync does not send the data anywhere else.
[`culture-rules`](https://github.com/agentculture/culture-rules) workflows
pick it up from the landing zone and route it onward (Notion, the Culture
mesh, email, eidetic memory, …). The landing-zone layout and the event stream
below are the stable contract they build against.

Non-goals: re-running speech-to-text (Pocket's transcript is the source of
truth), routing data downstream, and any UI.

## Status

**Built (0.11.0); live deploy pending.** Anything marked *(planned)* below is
not on disk yet. The build brief is
[issue #1](https://github.com/agentculture/pocketsync/issues/1). The operator
deploy and acceptance run are
[issue #3](https://github.com/agentculture/pocketsync/issues/3).

| Area | State |
|------|-------|
| Agent-first CLI (identity and introspection verbs) | Built |
| Data model and content hash (`pocketsync/model.py`) | Built |
| Settings and secrets loader (`pocketsync/config.py`) | Built |
| Local file sink (`pocketsync/sinks/local.py`) | Built |
| Mongo sink and enrichment CAS writes (`pocketsync/sinks/mongo.py`) | Built |
| Sync-state store (`pocketsync/state.py`) | Built |
| Vector index manager (`pocketsync/vindex.py`) | Built |
| `events.jsonl` outbox and events-cli publisher (`pocketsync/events.py`) | Built |
| Default taxonomy (`pocketsync/enrich/default_taxonomy.toml`) | Built |
| Mongo deploy stack (`deploy/mongo/`) | Built (see [operations guide](docs/operations/pocketsync-mongo.md)) |
| Pocket source adapter, official API (`pocketsync/pocket/`) | Built |
| Webhook HTTP server with a durable inbox (`pocketsync/webhook/`) | Built |
| Landing engine, Pocket to sinks (`pocketsync/sync.py`) | Built |
| Enrichment worker, embeddings and labels (`pocketsync/enrich/`) | Built |
| Reconciler and backfill (`pocketsync/reconcile.py`) | Built |
| `serve` composition (`pocketsync/serve.py`) | Built |
| CLI verbs `auth`, `list`, `sync`, `watch`, `serve`, `status`, `reembed` | Built |
| systemd and cloudflared deploy files | Built (see the [culture.dev runbook](docs/operations/pocketsync-culture-dev.md)) |
| Live deploy and acceptance run | *(planned)*: [issue #3](https://github.com/agentculture/pocketsync/issues/3) |
| S3 sink | *(planned)* |
| Example culture-rules rule consuming the events | *(planned)* |

**Pocket API access uses the official API.** Pocket has an official public API
at `public.heypocketai.com/api/v1/public/*`, authenticated with a static
`Authorization: Bearer pk_...` key (Settings → Developer → API Keys), plus signed
personal webhooks. The earlier reverse-engineered `app.heypocket.com` route with
an hourly Firebase ID token is superseded and not used: Pocket's Terms of
Service bar automated access "for unauthorized purposes". See the spec in
`docs/specs/2026-10-14-pocket-to-mongodb-near-realtime.md`.

## Audiences

- **Operator** (whoever runs pocketsync): deploys the Mongo stack, sets the
  secrets below, runs `pocketsync serve` (sync and enrichment), and reads
  `pocketsync status`. Start with the
  [operations guide](docs/operations/pocketsync-mongo.md).
- **Consumers** (culture-rules workflows and Culture mesh agents): read the
  stores and react to the events. The
  [recordings document](#the-recordings-document),
  [`sync_state`](#the-sync_state-collection) and
  [event](#events-contract) sections below are the public contract. Treat
  changes to them like API changes. Consumers never call Pocket and never need
  the Pocket or webhook secrets.

## CLI

Write verbs are dry-run by default and commit only with `--apply`. Every verb
takes `--json`.

| Verb | What it does |
|------|-----------------|
| `pocketsync auth` | Verify the Pocket API key with one list call. |
| `pocketsync list` | List remote recordings and their sync status. |
| `pocketsync sync [--since DATE] [--no-audio] [--no-enrich] [--apply]` | One reconcile or backfill pass into the sinks. |
| `pocketsync watch [--interval S]` | Reconciler-only loop until SIGTERM or Ctrl-C. |
| `pocketsync serve [--port P]` | Webhook receiver, landing, reconciler and enrichment in one process. |
| `pocketsync status` | Watermark, last run, webhook health, enrichment backlog and last errors. |
| `pocketsync reembed [--apply]` | Queue recordings embedded with another model for re-embedding. |

Sync is incremental and idempotent. A re-run with no remote changes does
nothing. A content hash detects when Pocket re-processes a transcript or
summary, and that recording is re-synced. Audio is downloaded once per
recording; if it is missing (`--no-audio`, or a failed download), a later
pass with audio fetches it.

## Data contract

Two stores hold the data. Both are written only by pocketsync; consumers read.

### Mongo (primary)

Database `pocketsync` (default) on the MongoDB Atlas Local stack in
`deploy/mongo/`. Collections:

| Collection | Contents |
|------------|----------|
| `recordings` | One document per Pocket recording; `_id` is the Pocket recording id. |
| `sync_state` | One bookkeeping document, `{_id: "state"}`. |
| `webhook_dedupe` | One `{_id: <dedupe key>, seen_at}` document per webhook delivery seen. |
| `pocketsync_meta` | `{_id: "vindex", model, dims}`: the embedding model and dimensions the vector indexes were built for. |

### The recordings document

Fields come from `pocketsync/sinks/mongo.py`. Timestamps are ISO-8601 UTC
strings.

| Group | Fields |
|-------|--------|
| Content | `title`, `description`, `recording_at`, `duration` (seconds), `language`, `state`, `folder_id`, `tags[]`, `speakers[]`, `segments[]` (`start`, `end`, `text`, `speaker`, `speaker_name`), `summary` (markdown), `action_items[]` (`text`, `done`, `owner`) |
| Source | `source.created_at`, `source.updated_at`: Pocket's own timestamps |
| Identity | `content_hash` (SHA-256 of the canonical mapped content; Pocket's raw wire payload is excluded), `landed_at` (first write), `updated_at` (pocketsync's write time; moves only when content changes) |
| Files | `files`: refs `{rel_path, size, sha256}` for `transcript_json`, `transcript_md`, `summary_md`, `audio` and `meta`; each is `null` when absent |
| Tombstone | `deleted_in_pocket` (bool), `deleted_at` |
| Merge | `merged_into` (surviving recording id), `merged_at` |
| Enrichment | `enrichment.status` (`pending`, `done` or `error`), `attempts`, `model`, `dims`, `labels[]`, `filters`, `taxonomy_version`, `summary_vector[]`, `chunks[]` (`index`, `start`, `end`, `text`, `vector`), `enriched_at`, `error` |

Behavior consumers can rely on:

- Pocket's raw payload and fetch time are not stored.
- A content change rewrites the content group, sets `enrichment.status` to
  `pending`, resets `attempts` to 0 and removes every derived enrichment
  field, so a stale vector never describes new content.
- Enrichment is written compare-and-set on `content_hash`: a result computed
  from superseded content is dropped.
- Deleted and merged recordings are tombstoned or marked, never purged.
  Filter on `deleted_in_pocket` and `merged_into` when querying.
- Mongo stores file references only, never audio bytes.

Example, with synthetic values and truncated vectors:

```json
{
  "_id": "rec_0001",
  "title": "Weekly sync",
  "description": "",
  "recording_at": "2026-01-15T09:30:00Z",
  "duration": 1260.5,
  "language": "en",
  "state": "completed",
  "folder_id": null,
  "tags": ["work"],
  "speakers": ["Speaker 1", "Speaker 2"],
  "segments": [
    {"start": 0.0, "end": 4.2, "text": "Let's get started.",
     "speaker": "s1", "speaker_name": "Speaker 1"}
  ],
  "summary": "- Agreed to ship on Friday.",
  "action_items": [{"text": "Send the release notes", "done": false, "owner": null}],
  "source": {"created_at": "2026-01-15T09:30:05Z", "updated_at": "2026-01-15T10:02:11Z"},
  "content_hash": "<64 hex chars>",
  "files": {
    "transcript_json": {"rel_path": "2026/01/rec_0001/transcript.json", "size": 812,
                        "sha256": "<64 hex chars>"},
    "transcript_md": null, "summary_md": null, "audio": null, "meta": null
  },
  "landed_at": "2026-01-15T10:03:00Z",
  "updated_at": "2026-01-15T10:03:00Z",
  "deleted_in_pocket": false,
  "deleted_at": null,
  "merged_into": null,
  "merged_at": null,
  "enrichment": {
    "status": "done",
    "attempts": 0,
    "model": "example-embed-model",
    "dims": 768,
    "labels": ["meeting"],
    "filters": {"has_action_items": true},
    "taxonomy_version": "1",
    "summary_vector": [0.012, -0.034, "…768 floats"],
    "chunks": [
      {"index": 0, "start": 0.0, "end": 60.0, "text": "Let's get started. …",
       "vector": [0.021, 0.007, "…768 floats"]}
    ],
    "enriched_at": "2026-01-15T10:03:20Z",
    "error": null
  }
}
```

The enrichment worker writes `filters` from the lobes decider as
`{filter_name: confidence}`. The store accepts it as an opaque value, and the
label taxonomy is still open, so treat its inner shape as provisional.

### Vector search

`pocketsync/vindex.py` creates two Atlas `vectorSearch` indexes on
`recordings`, both cosine similarity, 768 dimensions by default:

| Index | Vector path | Filter fields |
|-------|-------------|---------------|
| `recordings_summary_vec` | `enrichment.summary_vector` | `enrichment.labels`, `deleted_in_pocket`, `recording_at` |
| `recordings_chunk_vec` | `enrichment.chunks.vector` (`nestedRoot` `enrichment.chunks`) | same |

A model/dimension guard refuses to mix vector spaces. Both indexes were
verified against a real atlas-local 8.0.34 (`tests/integration/`). On the
`nestedRoot` chunk index, recording-level filters such as `enrichment.labels`
must go in `$vectorSearch`'s **`parentFilter`**, not `filter`; under `filter`
they silently match nothing. Regular indexes cover `enrichment.status`,
`deleted_in_pocket` and `recording_at`.

### The `sync_state` collection

A single document, `{_id: "state"}`, from `pocketsync/state.py`:

| Field | Meaning |
|-------|---------|
| `watermark` | Cursor: how far Pocket has been read. |
| `last_run` | `{started, finished, stats}` of the last sync run. |
| `errors[]` | Bounded (last 50) list of `{where, message, at}`, newest last. |
| `counters` | Named integers, for example `webhook_missed` (deliveries the reconciler found that no webhook announced). |
| `last_webhook_at` | Time of the last accepted webhook. |

`webhook_dedupe` makes webhook handling idempotent: inserting an existing key
fails on `_id`, which reports the delivery as a duplicate.

### Local files (optional sink)

Same content as a directory tree, per `pocketsync/sinks/local.py`:

```text
<root>/
  events.jsonl                     # the event outbox (below)
  events.jsonl.published           # publisher progress (below)
  <YYYY>/<MM>/<recording-id>/
    meta.json                      # manifest, written LAST
    transcript.json                # {id, segments[]}
    transcript.md
    summary.md
    audio.<ext>                    # only when audio is requested
```

`<root>` is `POCKETSYNC_FILES_ROOT`. Every file is written atomically and
`meta.json` last, so a reader that sees `meta.json` never sees a half-written
recording. `meta.json` carries `id`, `title`, `created_at`, `updated_at`,
`recording_at`, `duration`, `language`, `content_hash`, `synced_at`,
`deleted_in_pocket`, `deleted_at`, `merged_into`, `merged_at` and `files`
(refs as above, without `meta`).

## Events contract

When a recording is new or changed, and its enrichment is done, pocketsync
appends one event to `<files root>/events.jsonl`. This is the handoff to
culture-rules and the mesh. Per `pocketsync/events.py`, one JSON object per
line:

```json
{
  "event_id": "00000000-0000-4000-8000-000000000001",
  "type": "pocketsync.recording.enriched",
  "recording_id": "rec_0001",
  "content_hash": "<64 hex chars>",
  "change": "created",
  "sinks": {
    "mongo": {"collection": "recordings", "doc_id": "rec_0001"},
    "files": {"meta": "2026/01/rec_0001/meta.json"}
  },
  "enrichment": {"labels": ["meeting"], "taxonomy_version": "1", "model": "example-embed-model"},
  "emitted_at": "2026-01-15T10:03:21Z"
}
```

| Field | Meaning |
|-------|---------|
| `event_id` | UUID; equals the envelope `id` on the broker. Dedupe on it. |
| `type` | Always `pocketsync.recording.enriched`. |
| `recording_id`, `content_hash` | Which recording and which version of its content. |
| `change` | `created` the first time a recording id is emitted, `updated` after. |
| `sinks.mongo` | Collection and document id to read the full record from. |
| `sinks.files` | Name to path relative to the files root. |
| `enrichment` | Labels (type), taxonomy version and model. |
| `emitted_at` | RFC 3339 UTC. |

Guarantees:

- A given `(recording_id, content_hash)` is appended at most once.
- The file is append-only. Each append is one write plus fsync. A crash can
  leave a torn final line, which readers ignore and the next append heals.
- Single writer: run one sync writer per files root.
- Optional MQTT publish through
  [`events-cli`](https://github.com/agentculture/events-cli)
  (`pip install 'pocketsync[events]'`): the outbox is drained at QoS 1 on
  topic `events/pocketsync/recording/enriched`, with the event as the envelope
  data and `event_id` as the envelope `id`. Progress is the byte offset in the
  `events.jsonl.published` sidecar. Delivery is **at-least-once**, so
  consumers must dedupe on the envelope `id`.

The example culture-rules rule that consumes these events is *(planned)*.

## Configuration and secrets

Settings resolve from environment variables, then an optional TOML file
(`POCKETSYNC_CONFIG`), then defaults (`pocketsync/config.py`). Secrets are
never printed in `repr` or error messages.

| Secret | Variable | Used for | Where it lives |
|--------|----------|----------|----------------|
| Pocket API key (`pk_…`) | `POCKETSYNC_API_KEY` | Reading recordings from Pocket | Environment or git-ignored config |
| Webhook secret | `POCKETSYNC_WEBHOOK_SECRET` | Verifying incoming Pocket webhooks | Environment or git-ignored config |
| Gateway API key | `POCKETSYNC_GATEWAY_API_KEY` | The lobes gateway used for embeddings and labels | Environment or git-ignored config |
| Mongo credentials | `MONGODB_INITDB_ROOT_USERNAME`, `MONGODB_INITDB_ROOT_PASSWORD` in `deploy/mongo/.env`, and `POCKETSYNC_MONGO_URI` which embeds them | Creating the database root user, and connecting to it | `deploy/mongo/.env` (git-ignored; copy `.env.example`) and the environment |

A Mongo URI looks like `mongodb://<user>:<password>@localhost:27019/` and
the stack only listens on loopback. Use placeholders in anything you commit.

Non-secret settings:

| Variable | Default | Meaning |
|----------|---------|---------|
| `POCKETSYNC_FILES_ROOT` | `$HOME/.local/share/pocketsync/files` | Root of the local file sink and the event outbox. |
| `POCKETSYNC_GATEWAY_URL` | `http://localhost:8001/v1` | Gateway base URL. |
| `POCKETSYNC_WEBHOOK_PORT` | `8787` | Webhook server port. |
| `POCKETSYNC_RECONCILE_INTERVAL` | `300` | Reconciler interval in seconds. |
| `POCKETSYNC_ENRICHMENT_CONCURRENCY` | `2` | Parallel enrichment jobs. |
| `POCKETSYNC_TAXONOMY_PATH` | built-in | Taxonomy TOML; default is `pocketsync/enrich/default_taxonomy.toml` (labels: meeting, call, idea, todo-reminder, personal-note, lecture-learning, other). |
| `POCKETSYNC_CONFIG` | unset | Path to the optional TOML config. |
| `POCKETSYNC_DATA_DIR` | `$HOME/.local/share/pocketsync/mongo` | Host data root for the Mongo stack (compose only). |

## Credentials and privacy

This repository is public. Credentials belong in environment variables, the
OS keyring, or a git-ignored config file. They are never committed. Tests use
a fake Pocket adapter, in-memory or mongomock stores, and synthetic fixtures
only, never data from real recordings.

## Quickstart (development)

```bash
uv sync
uv run pytest -n auto                 # run the test suite
uv run pocketsync whoami              # identity from culture.yaml
uv run pocketsync learn               # self-teaching prompt (add --json)
uv run teken cli doctor . --strict    # the agent-first rubric gate CI runs
```

## CLI available today

| Verb | What it does |
|------|--------------|
| `whoami` | Report this agent's nick, version, backend, and model from `culture.yaml`. |
| `learn` | Print a structured self-teaching prompt. |
| `explain <path>` | Markdown docs for any noun/verb path. |
| `overview` | Read-only descriptive snapshot of the agent. |
| `doctor` | Check the agent-identity invariants (prompt-file-present, backend-consistency). |
| `cli overview` | Describe the CLI surface itself. |

Every command supports `--json`. Results go to stdout, errors/diagnostics to
stderr (never mixed). Exit codes: `0` success, `1` user error, `2` environment
error, `3+` reserved.

## As a Culture mesh agent

pocketsync is also an AgentCulture mesh agent, provisioned from
`culture-agent-template`:

- **A mesh identity**: `culture.yaml` (`suffix: pocketsync`,
  `backend: claude`) and the matching resident prompt, `CLAUDE.md`.
- **The guildmaster skill kit** under `.claude/skills/`, vendored
  cite-don't-import. See [`docs/skill-sources.md`](docs/skill-sources.md).
- **CI and deploy**: pytest, lint, a secret scan, the agent-first rubric
  gate, a four-harness smoke check, and PyPI Trusted Publishing.

### Prompt files by harness

Four harnesses, four root files, no shared base. Each file is read by exactly
one harness:

| Harness | File(s) |
|---------|---------|
| Claude Code | [`CLAUDE.md`](CLAUDE.md) |
| Pi / associate | [`AGENTS.override.md`](AGENTS.override.md) + [`.pi/SYSTEM.md`](.pi/SYSTEM.md) |
| colleague | [`AGENTS.colleague.md`](AGENTS.colleague.md) |
| Qwen Code | [`QWEN.md`](QWEN.md) |

`CLAUDE.md` is the fullest write-up of the repo's conventions; read it first.
There is intentionally **no `AGENTS.md`**, because a shared base would leak
Claude-specific guidance into the other harnesses.

`culture.yaml`'s `backend` selects only the **mesh resident**, the one prompt
the Culture daemon loads. It does not limit which harness you can run
interactively in the clone; all four stay live. See
[`docs/harness-selection.md`](docs/harness-selection.md) and
[`docs/automation-contract.md`](docs/automation-contract.md).

### The `gate` section

`culture.yaml` carries a top-level `gate` section that declares how to set up
and test this repo, so tooling (for example a PR-fixer that re-runs the checks
before pushing) needs no per-repo knowledge:

```yaml
gate:
  setup:
  - [uv, sync]
  test:
  - [uv, run, pytest, -n, auto]
```

`setup` and `test` are each a list of commands, and each command is an argv
list (never a shell string), run in order from the repo root. Culture's loader
ignores unknown top-level keys, so the section is safe alongside `agents`.

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
