"""``pocketsync learn`` — the learnability affordance.

Prints a structured self-teaching prompt. Must satisfy the agent-first rubric:
>=200 chars and mention purpose, command map, exit codes, --json, and explain.
"""

from __future__ import annotations

import argparse

from pocketsync import __version__
from pocketsync.cli._output import emit_result

_TEXT = """\
pocketsync — sync Pocket voice-recorder data to local disk or S3.

Purpose
-------
Pull recordings, transcripts (STT), summaries and action items out of the Pocket
cloud into a landing zone we own (local disk and/or S3), and emit an append-only
events.jsonl that culture-rules workflows trigger on. pocketsync does not route
data further.

Status: the sync verbs (auth, list, sync, watch, status) are planned and not yet
available; today the CLI carries the agent identity and introspection verbs
below. See https://github.com/agentculture/pocketsync/issues/1.

Commands
--------
  pocketsync whoami             Identity from culture.yaml.
  pocketsync learn              This self-teaching prompt.
  pocketsync explain <path>...  Markdown docs for any noun/verb path.
  pocketsync overview           Descriptive snapshot of the agent.
  pocketsync doctor             Check the agent-identity invariants.
  pocketsync cli overview       Describe the CLI surface itself.

Machine-readable output
-----------------------
Every command supports --json. Errors in JSON mode emit
{"code", "message", "remediation"} to stderr. Stdout and stderr never mix.

Exit-code policy
----------------
  0 success
  1 user-input error (bad flag, bad path, missing arg)
  2 environment / setup error
  3+ reserved

More detail
-----------
  pocketsync explain pocketsync
"""


def _as_json_payload() -> dict[str, object]:
    return {
        "tool": "pocketsync",
        "version": __version__,
        "purpose": "Sync Pocket recordings, transcripts and summaries to local disk or S3.",
        "commands": [
            {"path": ["whoami"], "summary": "Identity probe from culture.yaml."},
            {"path": ["learn"], "summary": "Self-teaching prompt."},
            {"path": ["explain"], "summary": "Markdown docs by path."},
            {"path": ["overview"], "summary": "Descriptive snapshot of the agent."},
            {"path": ["doctor"], "summary": "Check the agent-identity invariants."},
            {"path": ["cli", "overview"], "summary": "Describe the CLI surface."},
        ],
        "exit_codes": {
            "0": "success",
            "1": "user-input error",
            "2": "environment/setup error",
        },
        "json_support": True,
        "explain_pointer": "pocketsync explain <path>",
    }


def cmd_learn(args: argparse.Namespace) -> int:
    if getattr(args, "json", False):
        emit_result(_as_json_payload(), json_mode=True)
    else:
        emit_result(_TEXT, json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "learn",
        help="Print a structured self-teaching prompt for agent consumers.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_learn)
