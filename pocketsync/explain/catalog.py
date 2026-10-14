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
own (local disk and/or S3) and emits an append-only `events.jsonl` that
culture-rules workflows trigger on. It does not route data any further.

The sync verbs (`auth`, `list`, `sync`, `watch`, `status`) are planned (issue
#1); today the CLI carries the agent-first identity and introspection verbs.

## Verbs

- `pocketsync whoami` — identity probe from `culture.yaml`.
- `pocketsync learn` — structured self-teaching prompt.
- `pocketsync explain <path>` — markdown docs for any noun/verb.
- `pocketsync overview` — descriptive snapshot of the agent.
- `pocketsync doctor` — check the agent-identity invariants.
- `pocketsync cli overview` — describe the CLI surface.

## Exit-code policy

- `0` success
- `1` user-input error
- `2` environment / setup error
- `3+` reserved

## See also

- `pocketsync explain whoami`
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
skills-present check. Exits 1 when unhealthy.

prompt-file-present requires the *resident* prompt the declared backend
actually reads. Other harness prompt files recognized under the same backend
name (`AGENTS.override.md`, `.pi/SYSTEM.md`, `QWEN.md`) belong to
interactively available harnesses the mesh daemon never loads; they are
reported by the informational harness-prompts check and never substituted.

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
}
