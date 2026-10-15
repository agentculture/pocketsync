# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

`pocketsync` is the **sync bridge for the Pocket AI voice recorder**
(<https://heypocket.com>). Pocket records audio and produces speech-to-text
transcripts, summaries and action items in **its own cloud**. pocketsync pulls
that data out into stores we own: **local disk**, **S3** (or any
S3-compatible bucket) and a dedicated **MongoDB** (`pocketsync-mongo`),
kept near-realtime. It then enriches each recording and emits one handoff
event. Routing onward to Notion, the mesh, email and so on is the job of
[`culture-rules`](https://github.com/agentculture/culture-rules) workflows,
which trigger on pocketsync's events and read `pocketsync-mongo`. So the
output layout, the mongo document shape and the event stream are a **public
contract** other code builds against; treat changes to them like API changes.

Non-goals: no re-running STT (Pocket's transcript is the source of truth; we
store the audio as files), no routing to downstream destinations, no UI.

## Status — sync engine built (0.11.0), live deploy pending

The Pocket-to-MongoDB build is on disk as of 0.11.0. It includes:

- the official-API Pocket adapter (`pocketsync/pocket/`);
- the signed webhook receiver (`pocketsync/webhook/`), with a durable inbox
  in sync state;
- the landing engine (`pocketsync/sync.py`);
- the `local` and `mongo` sinks (`pocketsync/sinks/`) and sync state
  (`pocketsync/state.py`);
- the reconciler and backfill (`pocketsync/reconcile.py`);
- enrichment (`pocketsync/enrich/`) and the vector-index manager
  (`pocketsync/vindex.py`);
- the event outbox and publisher (`pocketsync/events.py`), and the `serve`
  composition (`pocketsync/serve.py`);
- the CLI verbs `auth`, `list`, `sync`, `watch`, `serve`, `status` and
  `reembed`, next to the introspection verbs (`whoami`, `learn`, `explain`,
  `overview`, `doctor`, `cli overview`).

Deploy files are in `deploy/`, and the runbooks are in `docs/operations/`.

Still *(planned)*:

- the `s3` sink;
- an example culture-rules rule that consumes the events;
- the operator-side live deploy and acceptance run
  ([issue #3](https://github.com/agentculture/pocketsync/issues/3)).

Follow-ups are tracked in
[issue #4](https://github.com/agentculture/pocketsync/issues/4).

The requirements baseline is the converged spec
[`docs/specs/2026-10-14-pocket-to-mongodb-near-realtime.md`](docs/specs/2026-10-14-pocket-to-mongodb-near-realtime.md)
and its build plan in `docs/plans/`. [Issue #1](https://github.com/agentculture/pocketsync/issues/1)
is the original brief. `gh issue view 1` fails on the deprecated
Projects-classic field, so use:

```bash
gh issue view 1 --repo agentculture/pocketsync --json title,body,comments
```

**The API premise (resolved by the spec).** Pocket has an **official public
API**. It lives at `public.heypocketai.com/api/v1/public/*` and is authenticated
with a static `Authorization: Bearer pk_...` key that the operator creates in
the app under Settings → Developer → API Keys. Pocket also offers **personal
webhooks**, signed with HMAC-SHA256 over `{timestamp}.{rawBody}` (headers
`X-HeyPocket-Signature` and `X-HeyPocket-Timestamp`). Delivery is
at-least-once, with a 30 s timeout and 3 attempts, so receivers must be
idempotent.

**Superseded:** the earlier premise in this file and in issue #1, a
reverse-engineered `app.heypocket.com` API authenticated with an hourly
Firebase ID token, is **not used**. Pocket's Terms of Service bar automated
access "for unauthorized purposes", and the official key makes that path
unnecessary. Do not build a Firebase or refresh-token path.

## Architecture (per the spec)

This is built as described, except where an item is marked *(planned)*. New
code should slot into this shape rather than invent a parallel one.

- **Source adapter: Pocket**, behind an interface, using the official API
  with the `pk_` key. List recordings since a cursor (a backfill and
  reconciliation path), and fetch each recording's transcript (with speakers
  and timestamps), summary, action items and metadata, plus the audio. Nothing
  outside the adapter may depend on Pocket's wire format. Tests use a **fake
  Pocket adapter**.
- **Webhook receiver** for near-realtime. Pocket's signed personal webhooks
  are verified (HMAC-SHA256 over `{timestamp}.{rawBody}`) and handled
  idempotently. The receiver is reachable at
  `pocketsync.culture.dev/hooks/pocket` through a Cloudflare tunnel, with
  Access on and an exact-path Bypass for that one route. Deletes arrive as
  **tombstones**.
- **Sink adapters: `local`, `mongo` and `s3` *(planned)***, behind one
  interface so more can be added. The per-recording file layout for local
  (and later S3):
  `<root>/<YYYY>/<MM>/<recording-id>/{meta.json, transcript.json, transcript.md, summary.md, audio.*}`.
  Writes are **atomic, with `meta.json` written last**, so a reader that sees
  `meta.json` never sees a half-written recording. Audio is immutable per
  recording: it is downloaded once, and a failed download never blocks the
  text from landing. S3 *(planned)* uses the standard AWS credential chain.
  Tests use a fake Mongo (`mongomock`).
- **Mongo sink: `pocketsync-mongo`.** A dedicated container
  (`mongodb/mongodb-atlas-local`) bound to `127.0.0.1:27019`, with data on host
  bind mounts **outside the repo** so it survives container re-creation. One
  document per recording holds metadata, speaker-attributed transcript
  segments, summary, action items, the audio file path and hash, 768-d vectors
  and type labels. Writes are idempotent upserts keyed by recording id and
  content hash, and the store is queryable with `$vectorSearch` plus type
  filters. Audio is saved as **files**, not in Mongo.
- **Enrichment by pocketsync** through the lobes gateway: `gemma2-embed`
  768-d vectors for transcript chunks and the summary, and type labels from the
  lobes decider. Enrichment runs before the event is emitted.
- **Incremental, idempotent sync.** Persisted cursor/state. A re-run with no
  remote changes is a no-op. Pocket may re-process a transcript or summary, so
  updates are detected by **content hash** and re-synced.
- **Handoff contract.** **One event per recording after enrichment**,
  published via [`events-cli`](https://github.com/agentculture/events-cli)
  at QoS 1, backed by an append-only `events.jsonl` **outbox** so no event is
  lost if the broker is down. Each event carries the recording id, sink paths,
  content hash and `created`/`updated`. The schema is documented in
  `README.md` and is what culture-rules builds against.
- **CLI verbs**:
  - `auth` verifies the `pk_` key.
  - `list` shows remote recordings with their sync status.
  - `sync [--since DATE] [--no-audio] [--no-enrich] [--apply]` runs one
    reconcile or backfill pass.
  - `watch` runs the reconciliation loop only.
  - `serve` runs the webhook receiver, landing, the reconciler and
    enrichment in one process.
  - `status` shows the watermark, the last run, webhook health, the
    enrichment backlog and the last errors.
  - `reembed` re-queues recordings embedded with another model.

  **Write verbs are dry-run by
  default and commit only with `--apply`**, and every verb takes `--json`.

Runtime dependencies are currently **zero** (`dependencies = []`). Third-party
clients go behind optional extras and are imported lazily, so the core and the
`local` sink stay dependency-free. `pymongo` is behind `pocketsync[mongo]`, the
events-cli client is behind `pocketsync[events]`, and `boto3` will be behind
`pocketsync[s3]` *(planned)*. Test-only libraries such as `moto` or
`mongomock` go in the `dev` dependency group.

## Constraints that bite

- **This repo is public.** Never commit tokens, recordings, transcripts, or
  fixtures derived from real recordings. Test fixtures are **synthetic**.
  Credentials (the `pk_` API key, the webhook signing secret, Mongo and AWS
  credentials) live in env vars, the keyring or a git-ignored config.
- **`scripts/scan-secrets.py` runs in CI** (lint job). It flags token-shaped
  strings and secret-ish `key = value` assignments in any tracked file. It
  also flags **non-localhost URLs under `url`/`host`/`endpoint`/`baseUrl`
  keys in any file that parses as JSON**. A Pocket base URL in Python source
  is fine. The same URL in a committed JSON fixture or config fails CI, so
  synthetic JSON fixtures should use `localhost` or omit URL keys.
- PyPI/TestPyPI Trusted Publishing for `pocketsync` still has to be
  registered by the operator. Until then the publish jobs in
  `.github/workflows/publish.yml` fail on the PyPI side, which is expected.

## Commands

```bash
uv sync                                      # install (incl. dev group)
uv run pytest -n auto                        # full suite (what culture.yaml's gate runs)
uv run pytest tests/test_cli.py::test_whoami_json -v   # a single test
uv run pytest -n auto --cov=pocketsync --cov-report=term   # with coverage (fail_under = 60)

uv run black pocketsync tests && uv run isort pocketsync tests   # format (line length 100)
uv run flake8 pocketsync tests
uv run bandit -c pyproject.toml -r pocketsync
markdownlint-cli2 "**/*.md" "#node_modules" "#.local" "#.claude/skills" "#.teken"
python3 scripts/scan-secrets.py              # CI secret/endpoint gate
uv run teken cli doctor . --strict           # agent-first rubric gate (CI)
uv run python scripts/harness-smoke.py --stage config   # four-harness config check (CI)

uv run pocketsync --help                     # the CLI
```

CI (`.github/workflows/tests.yml`) runs: `test` (pytest with coverage, plus
the SonarCloud scan when `SONAR_TOKEN` is set), `lint` (all of the lint
commands above), `harness-smoke`, and on PRs `version-check`.

## How the CLI is built

The CLI is cited (cite-don't-import) from teken's `python-cli` reference.
Read several files together to see the contract:

- **Registration.** Each verb or noun is a module in
  `pocketsync/cli/_commands/` exposing `register(sub)`. It is wired in by
  hand in `_build_parser()` in `pocketsync/cli/__init__.py`. Handlers return
  `None`/`int`, or raise `CliError`.
- **Error contract.** Every failure raises `CliError(code, message,
  remediation)` from `pocketsync/cli/_errors.py`. `_dispatch()` wraps any
  other exception into one, so no traceback ever reaches the user.
  `_CliArgumentParser` routes argparse errors through the same path. It scans
  raw argv for `--json` *before* parsing, so even parse errors render as JSON.
  Exit codes: `0` success, `1` user error, `2` environment error, `3+`
  reserved.
- **Output contract.** `pocketsync/cli/_output.py`: results go to stdout via
  `emit_result`, and errors and diagnostics go to stderr via `emit_error` /
  `emit_diagnostic`. Never mix them. Text-mode errors print `error:` plus a
  `hint:` line, which the rubric requires.
- **`explain` catalog.** `pocketsync/explain/catalog.py` maps command-path
  tuples to markdown. Every new verb or noun needs an entry, and
  `tests/test_cli.py` checks that every catalog path resolves. When adding
  verbs, also update `learn.py` (text and JSON command map) and the `_VERBS`
  list in `overview.py`.
- **Rubric.** `teken cli doctor . --strict` enforces things like `--json` on
  every command, `learn` content, and any noun with action verbs exposing
  `overview` (that's why the `cli` noun exists). Planned nouns must keep it
  green.
- **Identity.** `whoami`/`doctor` find `culture.yaml` by walking up from
  the module file (not the cwd) and parse it without a YAML dependency. In a
  wheel install there is no `culture.yaml`, so `doctor` reports a single info
  check and exits 0.

## Identity and the four harness prompt files

`culture.yaml` declares `suffix: pocketsync`, `backend: claude`, so **this
file is the mesh resident prompt**. `pocketsync doctor` and `steward doctor`
check **prompt-file-present** and **backend-consistency** (`claude` ↔
`CLAUDE.md`).

There are four harnesses, each reading exactly one root prompt, with
deliberately **no shared `AGENTS.md`**. `harness-smoke.py` fails CI if a bare
`AGENTS.md` appears.

| Harness | File(s) |
|---------|---------|
| Claude Code | `CLAUDE.md` (this file) |
| Pi / associate | `AGENTS.override.md` (context) + `.pi/SYSTEM.md` (system prompt) |
| colleague | `AGENTS.colleague.md` |
| Qwen Code | `QWEN.md` |

When the project's description or state changes, **update all four, not just
this one**. The other three summarize this file and must not drift. The live
harness probes in `docs/harness-invocations.yaml` assert on file content:

- Claude and Qwen are asked whether their file "describes this project as
  pocketsync";
- Pi is asked about the lobes-cli `associate` attribution, which must stay in
  `.pi/SYSTEM.md` only.

`culture.yaml` selects only the *mesh resident*; all four harnesses stay
usable interactively (see `docs/harness-selection.md`).

The `gate` section of `culture.yaml` (`uv sync`, then `uv run pytest -n
auto`) is how external tooling sets up and tests this repo. Keep it accurate
if the setup or test command changes.

## Conventions

- **Every PR bumps the version**, even docs, config or CI: use the
  `version-bump` skill, which updates `pyproject.toml` and `CHANGELOG.md`.
  The `version-check` CI job blocks merge otherwise. `__version__` is read
  from package metadata, so `pyproject.toml` is the single source.
- **PRs go through the `cicd` skill** (`devex pr` plus SonarCloud gating;
  needs `devex` on PATH). Posts are signed `- pocketsync (Claude)`. The
  `cicd`/`communicate` scripts append that automatically from
  `culture.yaml`, so don't sign bodies they post.
- **Vendored skills are cited verbatim.** Don't reformat or edit anything
  under `.claude/skills/`. Fixes go upstream, then re-sync per
  `docs/skill-sources.md`. `.qwen/skills`, `.pi/skills` and
  `.colleague/skills` are symlinks to `.claude/skills`, and tests enforce
  that.
- **Reach for `ask-colleague` reflexively.** Run `review` on a non-trivial
  committed diff before opening a PR, and `explore` for a fresh read of an
  unfamiliar area. Both are read-only, in a throwaway worktree. `write
  --apply` / `write --pr` need the user's go-ahead. Treat its output as a
  second opinion to verify, not authority.
- **Worktrees you create go in `../.worktrees.pocketsync/<name>/`** with a
  work-scoped branch prefix (`sync/sinks`, not `agent/t2`). The vendored
  `assign-to-workforce` skill's example uses `../worktrees/` and
  `agent/<task-id>`; override both. Remove a worktree with `git worktree
  remove`. `ask-colleague`'s own temporary worktrees under `$TMPDIR` are
  exempt, because they clean themselves up.
- **Memory: `/recall` before, `/remember` after.** The wrappers default to
  scope `pocketsync` with `--visibility public`, which lands records in the
  committed in-repo store `.eidetic/memory/`. Use `--visibility private` for
  anything that must stay in `$HOME`, which includes anything about
  credentials or real recordings. Store decisions and gotchas, not what the
  repo already records.
- Keep docs grounded in what is on disk. Mark anything not yet built as
  *(planned)*, as this file does.
