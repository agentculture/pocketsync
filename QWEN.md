# QWEN.md

This file provides guidance to Qwen Code when working with code in this
repository. Qwen Code's context loader reads exactly `QWEN.md` and `AGENTS.md`
in a directory; this repo deliberately ships only `QWEN.md` — there is no
`AGENTS.md` here (each harness gets its own file; see "Prompt files by
harness" below), so this file is the sole source of project guidance for a
Qwen Code session.

## What this project is

`pocketsync` is the **sync bridge for the Pocket AI voice recorder**
(<https://heypocket.com>). It pulls recordings, speech-to-text transcripts,
summaries and action items out of Pocket's cloud into stores we own: local
disk, S3 and a dedicated MongoDB (`pocketsync-mongo`), kept near-realtime. It
enriches each recording and emits one handoff event.
[`culture-rules`](https://github.com/agentculture/culture-rules) workflows
route the data onward, so the on-disk layout, the mongo document shape and the
event stream are a public contract.

**Status: sync engine built (0.12.0); live deploy pending.** The shape below
is on disk except where an item is marked *(planned)*: the Pocket adapter, the
webhook receiver (with a durable inbox), landing, the `local` and `mongo`
sinks, the reconciler, enrichment (modes full/classify/embed), the event
outbox (`enriched` and `reclassified` events), the CLI verbs `auth`, `list`,
`sync`, `watch`, `serve`, `status` and `reembed`, the talk-to-recordings verbs
`enrich`, `search`, `get`, `transcript`, `recordings` and `actions`, the MCP
server `pocketsync mcp` (extra `pocketsync[mcp]`) and the private web page
`pocketsync web` (API in [`docs/web-api.md`](docs/web-api.md)). The operator deploy
and acceptance run are
[issue #3](https://github.com/agentculture/pocketsync/issues/3). The
requirements baseline is the spec
[`docs/specs/2026-10-14-pocket-to-mongodb-near-realtime.md`](docs/specs/2026-10-14-pocket-to-mongodb-near-realtime.md)
(original brief: [issue #1](https://github.com/agentculture/pocketsync/issues/1)).

**API premise (resolved by the spec).** Pocket has an official public API at
`public.heypocketai.com/api/v1/public/*`, authenticated with a static
`Authorization: Bearer pk_...` key (app Settings, Developer, API Keys), plus
personal webhooks signed with HMAC-SHA256 over `{timestamp}.{rawBody}`
(`X-HeyPocket-Signature`, `X-HeyPocket-Timestamp`; at-least-once, 30 s
timeout, 3 attempts). *Superseded:* the earlier reverse-engineered
Firebase-token path is not used; Pocket's Terms of Service bar automated
access "for unauthorized purposes".

Shape:

- A Pocket source adapter (official API) plus a signed-webhook receiver at
  `pocketsync.culture.dev/hooks/pocket` (Cloudflare tunnel, Access on with an
  exact-path Bypass); deletes arrive as tombstones.
- `local` and `mongo` sinks (plus `s3` *(planned)*) behind one interface. `pocketsync-mongo` is
  a dedicated `mongodb/mongodb-atlas-local` container on `127.0.0.1:27019`
  with host bind mounts outside the repo; audio is saved as files.
- Enrichment by pocketsync via the lobes gateway: `gemma2-embed` 768-d vectors
  and lobes decider type labels.
- Idempotent, content-hash incremental sync; one event per recording after
  enrichment via events-cli (QoS 1) with an `events.jsonl` outbox.
- Optional extras `pocketsync[mongo]`, `pocketsync[events]` and
  `pocketsync[mcp]` (plus `pocketsync[s3]` *(planned)*); the core stays
  dependency-free.
- One verb registry (`pocketsync/verbs.py`) feeds the CLI, the MCP server and
  the web API. The UI is only a thin client over that CLI/MCP surface. All
  recordings are searchable by default (operator decision, internal network);
  `exclude_sensitive` is opt-in; the chat model and embeddings run on the
  operator's lobes gateway; the site is behind Cloudflare Access with no
  Bypass and the server verifies the Access JWT on every request.

Non-goals: no re-running STT, no downstream routing, and no UI beyond a thin
client over the CLI/MCP surface. Verbs: `auth`, `list`, `sync`, `watch`,
`serve`, `status`, `reembed`, `enrich`, `search`, `get`, `transcript`,
`recordings`, `actions`, `mcp`, `web`; writes are dry-run by default,
`--apply` commits, and every verb takes `--json`. `gh issue view 1` errors on
a deprecated field; use
`gh issue view 1 --repo agentculture/pocketsync --json title,body,comments`.

## Constraints

- **The repo is public.** Never commit tokens, recordings, transcripts, or
  fixtures made from real recordings. Fixtures are synthetic.
- **S3 uses the standard AWS credential chain.** No bespoke secret store.
  The `pk_` API key and webhook secret come from env vars or the keyring,
  never from the repo.
- **`scripts/scan-secrets.py` runs in CI.** It fails on non-localhost URLs
  under `url`/`host`/`endpoint`/`baseUrl` keys in any JSON file, so JSON
  fixtures use `localhost`.
- **Runtime dependencies are zero today.** Put third-party clients behind
  optional extras (`pocketsync[mongo]`, `pocketsync[events]`, and
  `pocketsync[s3]` *(planned)*), imported lazily.

## Prompt files by harness

This repo's root carries one prompt file per agent harness, each read by
exactly one of them. There is no shared base file for them to inherit from:

- **Claude Code** → [`CLAUDE.md`](CLAUDE.md) (the fullest write-up; read it
  first if you are new to the repo).
- **Pi / associate** → [`AGENTS.override.md`](AGENTS.override.md) for context,
  plus [`.pi/SYSTEM.md`](.pi/SYSTEM.md) for its system prompt.
- **colleague** → [`AGENTS.colleague.md`](AGENTS.colleague.md).
- **Qwen Code** → this file.

## Identity

Declared in `culture.yaml`:

```yaml
agents:
- suffix: pocketsync
  backend: claude
```

`backend: claude` fixes the *mesh resident* prompt file to `CLAUDE.md`. The
mesh runtime reads that file, not this one. A Qwen Code session working here is
a separate, local tool session; it reads `QWEN.md` regardless of what
`culture.yaml` declares. The declaration and the resident prompt together
satisfy the two invariants `steward doctor` verifies: **prompt-file-present**
and **backend-consistency** (`claude` ↔ `CLAUDE.md`).

## The CLI

The CLI is cited (cite-don't-import) from teken's `python-cli` reference, so
the runtime package has **no third-party dependencies**. `teken` is a dev
dependency only. Verbs available today:

- `pocketsync whoami` — identity from `culture.yaml`.
- `pocketsync learn` — structured self-teaching prompt.
- `pocketsync explain <path>` — markdown docs for any noun/verb.
- `pocketsync overview` — descriptive snapshot of the agent.
- `pocketsync doctor` — check the agent-identity invariants.
- `pocketsync cli overview` — describe the CLI surface itself.

Conventions:

- Every command supports `--json`.
- Results go to stdout, and errors and diagnostics go to stderr (never mixed).
- Exit codes are `0` success, `1` user error, `2` environment error, `3+`
  reserved.
- New verbs need an `explain` catalog entry (`pocketsync/explain/catalog.py`).
- The agent-first rubric is enforced in CI by `teken cli doctor . --strict`.

## Skills

`.claude/skills/` vendors the **canonical guildmaster skill kit**
(cite-don't-import), and `.qwen/skills` is a symlink to it. Provenance and the
re-sync procedure live in `docs/skill-sources.md`. Do not reformat or edit
vendored scripts; re-sync from guildmaster instead.

## Conventions

- **Every PR bumps the version.** This applies even to docs, config and CI.
  Use the `version-bump` skill; the `version-check` CI job blocks merge
  otherwise.
- **Tests**: `uv run pytest -n auto`. A single test:
  `uv run pytest tests/test_cli.py::test_whoami_json`.
- **Lint**: black, isort, flake8 (line length 100), bandit, markdownlint,
  `scripts/scan-secrets.py`.
- **Deploy**: pushing to `main` publishes to PyPI via Trusted Publishing
  (`.github/workflows/publish.yml`), and PRs do a TestPyPI dry-run. The PyPI
  Trusted Publisher still has to be registered by the operator.

## Layout

```text
pocketsync/               agent-first CLI (cited from teken's python-cli reference)
  cli/                    parser, error/output contract, _commands/ (verbs)
  explain/                markdown catalog for `explain`
tests/                    pytest smoke + introspection tests
scripts/                  scan-secrets.py, harness-smoke.py (CI gates)
.claude/skills/           vendored guildmaster skill kit (cite-don't-import)
docs/                     skill provenance, harness selection/automation contract
culture.yaml              mesh identity (suffix + backend) + gate commands
.github/workflows/        tests + deploy (PyPI Trusted Publishing)
```

This file describes the repository **as it exists on disk today**. When you
edit, keep claims grounded in checked-in reality; if a section drifts ahead of
reality, mark it `(planned)`. For the full set of workflow conventions
(worktree layout, memory discipline, `ask-colleague` usage), see
[`CLAUDE.md`](CLAUDE.md). Those conventions apply to work in this repo
regardless of which harness is doing it.
