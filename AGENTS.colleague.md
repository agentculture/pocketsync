# Colleague Resident — `pocketsync`

You are a colleague session working in the pocketsync repo — reading
this file because colleague's prompt cascade resolves it here, not because
`culture.yaml` selected you. That declaration says `backend: claude`, so
`CLAUDE.md` is this repo's *mesh resident* prompt; colleague remains fully
usable interactively over the same clone, and this file is what it loads when
you run it. A clone that declares `backend: colleague` promotes this file to
its resident prompt as well — the guidance below holds either way.

Your job is to assist with scoped tasks delegated by the operator or peer
agents, using the colleague tool-loop (`read_file` / `write_file` /
`edit_file` / `list_dir` / `run_command` / `finish`).

## The prompt cascade (and what this repo actually ships)

colleague concatenates up to three files, in order, as its prompt cascade:

1. `AGENTS.md` — a shared base, if present.
2. `AGENTS.colleague.md` — this file.
3. `AGENTS.colleague.<sanitized-model>.md` — a model-specific override, if
   present.

**This repo ships only layer 2.** There is deliberately no `AGENTS.md` at the
root (a shared base across the four harness files was proposed and rejected —
each harness gets its own, unrelated file; see `CLAUDE.md`'s "Prompt files by
harness"), so the cascade for colleague in this repo starts and ends at this
file. There is also no `AGENTS.colleague.<sanitized-model>.md` — this repo
doesn't need per-model overrides today. If you add one of those files later,
update this section so the docs keep matching what's actually on disk.

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

`CLAUDE.md` is written for a Claude Code session working *on* the repo. It is
not your runtime prompt, but it is the fullest write-up of the
architecture, the CLI contract and the repo's conventions (worktree layout,
memory discipline, `ask-colleague` usage). Read it when a task needs more
context than fits here.

## How you work

- Prefer small, reversible steps; hand off via `finish` when done.
- Follow the operator's instructions and any skills loaded from
  `.colleague/skills/` when present.
- The vendored skills under `.claude/skills/` are cited **verbatim** from
  guildmaster — don't reformat or edit their scripts; a fix belongs upstream
  (see `docs/skill-sources.md` for the re-sync procedure).
- Every PR bumps the version (`version-bump` skill) — CI's `version-check` job
  blocks merge otherwise.
- The repo is public: never commit tokens, recordings, transcripts or
  fixtures made from real recordings. Tests run against a fake Pocket adapter,
  a fake S3 and a fake Mongo, with synthetic fixtures.
- Keep the CLI contract: `--json` on every verb, results to stdout and errors
  to stderr, `CliError` for failures, and an `explain` catalog entry for each
  new verb. Write verbs are dry-run by default and commit with `--apply`.
