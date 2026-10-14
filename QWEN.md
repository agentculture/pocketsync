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
summaries and action items out of Pocket's cloud. It lands them in a store we
own: **local disk** and/or **S3**. That landing zone is the whole lane.
[`culture-rules`](https://github.com/agentculture/culture-rules) workflows
pick the data up from there and route it elsewhere, so the on-disk layout and
the `events.jsonl` handoff stream are a public contract.

Non-goals: no re-running STT, no downstream routing, no UI.

## Status

**Scaffold only.** The repo is the `culture-agent-template` scaffold renamed
to `pocketsync`. The only CLI verbs on disk are the identity/introspection
ones listed below. The sync engine, Pocket adapter, sinks and events are
*(planned)*. The build brief is
[issue #1](https://github.com/agentculture/pocketsync/issues/1). `gh issue
view 1` errors on a deprecated field; use
`gh issue view 1 --repo agentculture/pocketsync --json title,body,comments`.

The Pocket API premise is **unverified**. No official API has been found; the
known path is a reverse-engineered `app.heypocket.com` API using a Firebase ID
token that expires roughly hourly. Verifying API access, persistent auth and
Terms of Service is the first deliverable.

## Planned shape (from issue #1)

- A **Pocket source adapter** behind an interface (the unofficial API will
  break; nothing else may depend on its internals).
- **`local` and `s3` sink adapters** behind one interface. Layout:
  `<root>/<YYYY>/<MM>/<recording-id>/{meta.json, transcript.json, transcript.md, summary.md, audio.*}`,
  written atomically with `meta.json` last.
- **Incremental, idempotent sync** with a persisted cursor and
  content-hash update detection.
- **An append-only `events.jsonl`** in the sink root, with one event per
  new or updated recording. The schema is documented in `README.md`.
- **Verbs**: `auth`, `list`, `sync`, `watch`, `status`. Writes are dry-run
  by default, `--apply` commits, and every verb takes `--json`.

## Constraints

- **The repo is public.** Never commit tokens, recordings, transcripts, or
  fixtures made from real recordings. Fixtures are synthetic.
- **S3 uses the standard AWS credential chain.** No bespoke secret store.
- **`scripts/scan-secrets.py` runs in CI.** It fails on non-localhost URLs
  under `url`/`host`/`endpoint`/`baseUrl` keys in any JSON file, so JSON
  fixtures use `localhost`.
- **Runtime dependencies are zero today.** Put third-party clients (e.g.
  `boto3`) behind an optional extra, imported lazily.

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
