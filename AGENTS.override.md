# AGENTS.override.md

This file is the **context layer** for the Pi harness (the `pi` CLI, and the
`associate` non-coding harness modelled on it) when it runs inside this repo.
Pi's CONTEXT loader concatenates `AGENTS.md` or `CLAUDE.md` from its user-level
config directory (see Pi's own docs), each parent directory, and the working
directory — but an `AGENTS.override.md`
present in a directory replaces that directory's `AGENTS.md`/`CLAUDE.md` entry
outright rather than adding to it. That is why this repo ships this file
instead of an `AGENTS.md`: Pi must **not** inherit `CLAUDE.md` (the Claude Code
guidance file) — the two harnesses read the same repository very differently,
and `CLAUDE.md` assumes a coding session with full repo-write authority that
Pi's non-coding lane does not have.

The identity and behavioral bounds for that lane — who Pi is here, what it may
and may not do — live one layer up, in Pi's **system prompt** file,
[`.pi/SYSTEM.md`](.pi/SYSTEM.md). That file replaces Pi's default
coding-assistant system prompt entirely. This file is project *context* only:
what the repo is and how it is laid out, not who is reading it.

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

Siblings: [`guildmaster`](https://github.com/agentculture/guildmaster)
(skills supplier), [`steward`](https://github.com/agentculture/steward)
(alignment), [`teken`](https://github.com/agentculture/teken) (the CLI
scaffolder this package is cited from), `culture-rules` (the downstream
consumer), and [`events-cli`](https://github.com/agentculture/events-cli)
(the MQTT event bus used for the handoff).

## Four harnesses, four files, no shared base

This repo's root carries one prompt file per harness, each read by exactly
one of them — there is deliberately no shared `AGENTS.md` base for them to
cascade from:

- **Claude Code** reads [`CLAUDE.md`](CLAUDE.md).
- **Pi / associate** reads this file (`AGENTS.override.md`) for context, plus
  [`.pi/SYSTEM.md`](.pi/SYSTEM.md) for its system prompt.
- **colleague** reads [`AGENTS.colleague.md`](AGENTS.colleague.md) (the start
  of colleague's own cascade — see that file).
- **Qwen Code** reads [`QWEN.md`](QWEN.md).

If you are reading this as a human, `CLAUDE.md` is the fullest write-up of the
repo's conventions and is the one to read first; the other three exist to keep
each non-Claude harness from silently inheriting Claude-specific instructions
it cannot act on the same way.

## Identity

Declared in `culture.yaml`:

```yaml
agents:
- suffix: pocketsync
  backend: claude
```

This repo's *mesh* resident runs on `backend: claude`, so `CLAUDE.md` is
the live resident prompt. A Pi session working in a clone of this repo is a
**local tool session**, not the mesh resident — it reads this file and
`.pi/SYSTEM.md` regardless of what `culture.yaml` declares, and running `pi`
here neither requires nor changes that declaration.

(A clone that wants `associate` as its *mesh* resident declares
`backend: colleague` with `model: associate` — see `docs/skill-sources.md`.
That is a per-clone choice; this repo does not ship it.)

## Layout (what you can read/find/summarize here)

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

## Conventions worth knowing before you answer a question about this repo

- The vendored skills under `.claude/skills/` are cited **verbatim** from
  guildmaster — never propose editing their scripts; the fix belongs upstream
  (`docs/skill-sources.md` has the re-sync procedure).
- The repo is public. Never quote, copy or summarize real Pocket
  credentials, recordings or transcripts into anything that could be
  committed; test fixtures are synthetic by rule.
- Write verbs (planned) are dry-run by default and commit only with
  `--apply`, so a dry-run is the safe way to answer "what would sync do?".
- Every PR bumps the version (`version-bump` skill); CI's `version-check` job
  blocks merge otherwise.
- This file describes the repo **as it exists on disk today**. If you are
  asked to update it, keep claims grounded in checked-in reality.
