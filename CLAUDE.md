# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

`pocketsync` is the **sync bridge for the Pocket AI voice recorder**
(<https://heypocket.com>). Pocket records audio and produces speech-to-text
transcripts, summaries and action items in **its own cloud**. pocketsync pulls
that data out into a store we own — **local disk** and/or **S3** (or any
S3-compatible bucket) — and stops there.

That landing zone is the whole lane. Fan-out to Notion, the mesh, email,
eidetic memory and so on is the job of
[`culture-rules`](https://github.com/agentculture/culture-rules) workflows,
which trigger on pocketsync's output. So the output layout and the event
stream are a **public contract** other code builds against; treat changes to
them like API changes.

Non-goals (from the build brief): no re-running STT (Pocket's transcript is
the source of truth; at most we store the audio), no routing to downstream
destinations, no UI.

## Status — scaffold only, build brief is issue #1

As of the last update to this file, the repo is still the
`culture-agent-template` scaffold renamed to `pocketsync`. **No Pocket, sink,
sync or event code exists yet.** The only verbs on disk are the agent-first
identity/introspection verbs (`whoami`, `learn`, `explain`, `overview`,
`doctor`, `cli overview`).

[Issue #1](https://github.com/agentculture/pocketsync/issues/1) is the build
brief and the requirements baseline. Read it before designing anything.
`gh issue view 1` fails on the deprecated Projects-classic field, so use:

```bash
gh issue view 1 --repo agentculture/pocketsync --json title,body,comments
```

**Verify the API premise first.** guildmaster found no official Pocket
developer API or bulk export. The only known path is a community skill that
reads `app.heypocket.com` through a **reverse-engineered** API, authenticated
with a **Firebase ID token** from a logged-in browser session. That token
expires about every hour. The first deliverable in #1 is a report on:

- any official API, webhook, integration (Zapier, Notion, …) or export;
- whether a Firebase **refresh token** can be persisted so sync runs
  unattended;
- Terms-of-Service limits on automated access. Flag them; don't ignore them.

Prefer an official path, even a narrow one, if one exists.

For new feature work the vendored devague chain applies: `/scope` → `/think`
→ `/challenge` (optional) → `/spec-to-plan` → `/assign-to-workforce` (or
build directly).

## Target architecture (planned — from #1, adjust once reality is verified)

None of this exists yet. It's the shape the brief asks for, so new code should
slot into it rather than invent a parallel one.

- **Source adapter: Pocket**, behind an interface. List recordings since a
  cursor. Fetch each recording's transcript (with speakers and timestamps if
  available), summary, action items and metadata, plus the audio behind a
  flag because it's large. The Pocket API is unofficial and *will* break, so
  nothing outside the adapter may depend on its wire format. Tests use a
  **fake Pocket adapter**.
- **Sink adapters: `local` and `s3`**, behind one interface so more can be
  added. Proposed per-recording layout:
  `<root>/<YYYY>/<MM>/<recording-id>/{meta.json, transcript.json, transcript.md, summary.md, audio.*}`.
  Writes are **atomic, with `meta.json` written last**, so a reader that sees
  `meta.json` never sees a half-written recording. S3 uses the standard AWS
  credential chain (env, profile or role) and no bespoke secret store. Tests
  use a **fake S3** (moto or a stub).
- **Incremental, idempotent sync.** Persisted cursor/state. A re-run with no
  remote changes is a no-op. Pocket may re-process a transcript or summary, so
  updates are detected by **content hash** and re-synced.
- **Handoff contract: `events.jsonl`.** An append-only file in the sink root
  with one event per new or updated recording, carrying the recording id, sink
  paths, content hash and `created`/`updated`. A hook or MQTT publish via
  [`events-cli`](https://github.com/agentculture/events-cli) is an optional
  addition. The schema is documented in `README.md` and is what culture-rules
  builds against. #1 also asks for one example culture-rules rule that
  consumes it.
- **CLI verbs**: `auth` (set up and verify credentials), `list` (remote
  recordings plus sync status), `sync [--to local|s3] [--since …]
  [--with-audio] [--apply]`, `watch` (poll loop or daemon), `status` (cursor,
  last run, errors). **Write verbs are dry-run by default and commit only with
  `--apply`**, and every verb takes `--json`.

Runtime dependencies are currently **zero** (`dependencies = []`). A
third-party client such as `boto3` should go behind an optional extra (for
example `pocketsync[s3]`) and be imported lazily, so the core and the
`local` sink stay dependency-free. Test-only libraries such as `moto` go in
the `dev` dependency group.

## Constraints that bite

- **This repo is public.** Never commit tokens, recordings, transcripts, or
  fixtures derived from real recordings. Test fixtures are **synthetic**.
  Credentials live in env vars, the keyring or a git-ignored config.
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
