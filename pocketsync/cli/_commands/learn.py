"""``pocketsync learn`` — the learnability affordance.

Prints a structured self-teaching prompt. Must satisfy the agent-first rubric:
>=200 chars and mention purpose, command map, exit codes, --json, and explain.
"""

from __future__ import annotations

import argparse

from pocketsync import __version__
from pocketsync.cli._output import emit_result

_TEXT = """\
pocketsync — sync Pocket voice-recorder data into MongoDB and local files.

Purpose
-------
Pull recordings, transcripts (STT), summaries and action items out of the Pocket
cloud into a landing zone we own: one MongoDB document per recording (enriched
with embeddings and labels for vector search) plus a local directory tree, and
an append-only events.jsonl outbox that culture-rules workflows trigger on.
pocketsync does not route data further.

Commands
--------
  pocketsync auth                 Verify the Pocket API key (one list call).
  pocketsync list                 Remote recordings + local sync status.
  pocketsync sync [--apply]       One reconcile pass (--since DATE: backfill).
  pocketsync watch                Reconciler-only loop (foreground).
  pocketsync serve                Webhook receiver + landing + reconciler +
                                  enrichment worker in one process.
  pocketsync status               Watermark, last run, webhook health, backlog.
  pocketsync reembed [--apply]    Queue recordings embedded with another model.
  pocketsync whoami               Identity from culture.yaml.
  pocketsync learn                This self-teaching prompt.
  pocketsync explain <path>...    Markdown docs for any noun/verb path.
  pocketsync overview             Descriptive snapshot of the agent.
  pocketsync doctor               Identity + runtime health checks.
  pocketsync cli overview         Describe the CLI surface itself.

Write verbs (sync, reembed) are dry runs unless --apply; a dry run writes
nothing. serve and watch run until SIGTERM or Ctrl-C.

Configuration
-------------
Environment variables (or a TOML file named by POCKETSYNC_CONFIG):
POCKETSYNC_API_KEY, POCKETSYNC_MONGO_URI, POCKETSYNC_WEBHOOK_SECRET,
POCKETSYNC_GATEWAY_API_KEY, POCKETSYNC_FILES_ROOT, POCKETSYNC_GATEWAY_URL,
POCKETSYNC_WEBHOOK_PORT, POCKETSYNC_RECONCILE_INTERVAL. Secrets are never printed.

Machine-readable output
-----------------------
Every command supports --json. Errors in JSON mode emit
{"code", "message", "remediation"} to stderr. Stdout and stderr never mix.

Exit-code policy
----------------
  0 success
  1 user-input error (bad flag, bad path, missing arg)
  2 environment / setup error (missing setting, Pocket/Mongo/gateway down)
  3+ reserved

More detail
-----------
  pocketsync explain pocketsync
  pocketsync explain sync
"""


def _as_json_payload() -> dict[str, object]:
    return {
        "tool": "pocketsync",
        "version": __version__,
        "purpose": (
            "Sync Pocket recordings, transcripts and summaries into MongoDB and local files."
        ),
        "commands": [
            {"path": ["auth"], "summary": "Verify the Pocket API key (one list call)."},
            {"path": ["list"], "summary": "Remote recordings with local sync status."},
            {"path": ["sync"], "summary": "One reconcile/backfill pass; dry run unless --apply."},
            {"path": ["watch"], "summary": "Reconciler-only loop until SIGTERM/Ctrl-C."},
            {
                "path": ["serve"],
                "summary": "Webhook receiver + landing + reconciler + enrichment worker.",
            },
            {"path": ["status"], "summary": "Watermark, last run, webhook health, backlog."},
            {"path": ["reembed"], "summary": "Queue re-embedding; dry run unless --apply."},
            {"path": ["whoami"], "summary": "Identity probe from culture.yaml."},
            {"path": ["learn"], "summary": "Self-teaching prompt."},
            {"path": ["explain"], "summary": "Markdown docs by path."},
            {"path": ["overview"], "summary": "Descriptive snapshot of the agent."},
            {"path": ["doctor"], "summary": "Identity + runtime health checks."},
            {"path": ["cli", "overview"], "summary": "Describe the CLI surface."},
        ],
        "exit_codes": {
            "0": "success",
            "1": "user-input error",
            "2": "environment/setup error",
        },
        "write_verbs": {"dry_run_default": True, "commit_flag": "--apply"},
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
