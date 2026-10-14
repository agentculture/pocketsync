# pocketsync

Sync Pocket voice-recorder recordings, transcripts (STT) and summaries from the
Pocket cloud to local disk or S3 - a durable landing zone that culture-rules
workflows fan out to anywhere.

## What it does

[Pocket](https://heypocket.com) records audio and produces transcripts,
summaries and action items in its own cloud. pocketsync copies that data into
a store you own:

- **local disk** (a directory tree), and/or
- **S3** (or any S3-compatible bucket).

That is the whole job. pocketsync does not send the data anywhere else.
[`culture-rules`](https://github.com/agentculture/culture-rules) workflows
pick it up from the landing zone and route it onward (Notion, the Culture
mesh, email, eidetic memory, …). The landing-zone layout and the event stream
below are the stable contract they build against.

Non-goals: re-running speech-to-text (Pocket's transcript is the source of
truth), routing data downstream, and any UI.

## Status

**Early: the sync engine is not built yet.** Today the package is the
agent-first CLI scaffold (identity and introspection verbs only). The build
brief is [issue #1](https://github.com/agentculture/pocketsync/issues/1).

**Pocket API access is unverified.** No official Pocket developer API or bulk
export has been found so far. The known community approach uses a
reverse-engineered `app.heypocket.com` API with a short-lived (about one hour)
Firebase ID token. Before the sync engine is built, issue #1 calls for a
report on:

- whether an official path exists;
- whether authentication can persist unattended;
- what Pocket's Terms of Service say about automated access.

## Planned CLI

Write verbs are dry-run by default and commit only with `--apply`. Every verb
takes `--json`.

| Verb | What it will do |
|------|-----------------|
| `pocketsync auth` | Set up and verify Pocket credentials. |
| `pocketsync list` | List remote recordings and their sync status. |
| `pocketsync sync [--to local\|s3] [--since …] [--with-audio] [--apply]` | Incrementally sync recordings into a sink. |
| `pocketsync watch` | Poll loop or daemon around `sync`. |
| `pocketsync status` | Show the cursor, the last run and errors. |

Sync is incremental and idempotent. A persisted cursor means a re-run with no
remote changes does nothing. A content hash detects when Pocket re-processes a
transcript or summary, and that recording is re-synced.

## Landing-zone contract (draft)

> **Draft.** This is the shape proposed in issue #1. It is not implemented
> yet and may change before the first release that writes data. Once
> released, changes to it are breaking changes for culture-rules consumers.

### Layout

One directory per recording, in the same layout on local disk and on S3:

```text
<root>/
  events.jsonl                     # append-only handoff log (below)
  <YYYY>/<MM>/<recording-id>/
    meta.json                      # manifest — written LAST
    transcript.json                # speakers + timestamps where available
    transcript.md
    summary.md
    audio.*                        # only with --with-audio
```

Every file is written atomically, and `meta.json` is written last. A consumer
that sees `meta.json` can rely on the rest of the recording being complete.

### `events.jsonl`

One JSON object per line, appended for each recording that is new or has
changed. Each event carries at least:

| Field | Meaning |
|-------|---------|
| recording id | Pocket's identifier for the recording. |
| sink paths | Where the recording's files landed. |
| content hash | Hash of the synced content; a new hash means an update. |
| `created` / `updated` | Timestamps for the recording / this sync. |

The exact field names will be fixed in this README when the sync engine
ships. Optionally, the same events may also be published over MQTT via
[`events-cli`](https://github.com/agentculture/events-cli).

## Credentials and privacy

This repository is public. Pocket credentials belong in environment variables,
the OS keyring, or a git-ignored config file. They are never committed. S3
access uses the standard AWS credential chain (environment, profile or
instance role). Tests use a fake Pocket adapter, a fake S3, and synthetic
fixtures only, never data from real recordings.

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
