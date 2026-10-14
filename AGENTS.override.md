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
summaries and action items out of Pocket's cloud into a store we own: local
disk and/or S3. [`culture-rules`](https://github.com/agentculture/culture-rules)
workflows route the data onward from that landing zone, so its layout and the
append-only `events.jsonl` handoff stream are a public contract (draft schema
in `README.md`).

**Status: scaffold only.** The repo is still the `culture-agent-template`
scaffold renamed to `pocketsync`. The sync engine, Pocket adapter, sinks and
events are *(planned)*, and none of that code exists yet. The build brief is
[issue #1](https://github.com/agentculture/pocketsync/issues/1). Its first
open question is whether Pocket can be accessed at all: no official API is
known, and the community approach uses a reverse-engineered API with an
hourly-expiring Firebase token. If you are asked about Pocket API access,
report what is verified and what is not.

Siblings: [`guildmaster`](https://github.com/agentculture/guildmaster)
(skills supplier), [`steward`](https://github.com/agentculture/steward)
(alignment), [`teken`](https://github.com/agentculture/teken) (the CLI
scaffolder this package is cited from), `culture-rules` (the downstream
consumer), and [`events-cli`](https://github.com/agentculture/events-cli)
(the optional MQTT event bus).

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
