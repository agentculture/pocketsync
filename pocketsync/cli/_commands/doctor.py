"""``pocketsync doctor`` — check the agent-identity invariants.

Mirrors the two invariants ``steward doctor`` verifies for a mesh agent:

* **prompt-file-present** — the repo declares an agent in ``culture.yaml`` and
  has the matching prompt file on disk;
* **backend-consistency** — the declared ``backend`` is one this template
  knows, and the *resident* prompt file that backend actually reads is on disk
  (``claude`` → ``CLAUDE.md``, ``colleague`` → ``AGENTS.colleague.md``,
  ``acp``/``codex``/``copilot`` → ``AGENTS.md``, ``gemini`` → ``GEMINI.md``).

Additionally a **harness-prompts** info check reports which *other* recognized
harness prompt files are present. Those files (``AGENTS.override.md``,
``.pi/SYSTEM.md``, ``QWEN.md``) belong to interactively available harnesses
that ride the same backend name; they are never accepted as substitutes for
the resident prompt, because the Culture daemon does not read them.

Plus a **skills-present** check (the vendored ``.claude/skills/`` kit). Read-only.

Runtime checks (appended after the identity checks; each carries ``status``
``ok``/``warn``/``fail``/``info`` and a remediation; only ``fail`` makes doctor unhealthy):

* **mongo_auth** -- ``ping`` with the configured ``POCKETSYNC_MONGO_URI`` (credentials
  included); failure is ``fail`` and the Mongo-dependent checks below become ``warn``;
* **gateway_reachability** -- ``GET <gateway root>/capabilities`` (the unauthenticated lobes
  capabilities document one level above ``gateway_url``'s ``/v1``; no model call, no key
  needed). Unreachable is ``fail``; reachable without ``gemma2-embed`` advertised, or with
  ``POCKETSYNC_GATEWAY_API_KEY`` unset, is ``warn``;
* **vector_index_model** -- :func:`pocketsync.vindex.check` against gemma2-embed/768; a
  mismatch is ``fail`` (run ``reembed --apply``); search indexes that cannot be listed (no
  Atlas Search) are ``warn``;
* **webhook_freshness** -- when ``POCKETSYNC_WEBHOOK_SECRET`` is set (serve is expected),
  ``warn`` if no verified webhook arrived in ``WEBHOOK_STALE_HOURS``; ``info`` otherwise;
* **webhook_missed**, **enrichment_backlog**, **recent_errors** -- the ``status`` health
  numbers, ``warn`` when non-zero / above ``BACKLOG_WARN`` / newer than a day.

When nothing is configured (no API key, Mongo URI, webhook secret or gateway key) a single
``runtime_config`` info check replaces them and nothing is probed, so a wheel install with
no configuration still exits 0.

Reports the rubric-shaped contract
``{healthy, checks: [{id, passed, severity, message, remediation}]}`` so the
agent-first rubric's bundle 7 passes. When run from a wheel install (no
``culture.yaml`` alongside the package), it reports a single info check and
exits 0 — there is nothing to diagnose.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from typing import Any

from pocketsync import config, vindex
from pocketsync.cli import _runtime
from pocketsync.cli._commands.whoami import find_culture_yaml, read_agent_fields
from pocketsync.cli._errors import CliError
from pocketsync.cli._output import emit_result
from pocketsync.enrich.gateway import EMBED_DIMS, EMBED_MODEL
from pocketsync.state import COUNTER_WEBHOOK_MISSED

#: Warn when serve is expected (webhook secret set) but no verified webhook arrived this long.
WEBHOOK_STALE_HOURS = 24
#: Warn when more than this many docs wait for enrichment.
BACKLOG_WARN = 100
#: Errors newer than this many hours make ``recent_errors`` warn.
RECENT_ERROR_HOURS = 24

#: Shared by every AGENTS.md-reading backend (codex, acp, copilot).
_AGENTS_MD = "AGENTS.md"

# backend → every prompt file RECOGNIZED under that backend name.
#
# Values are tuples because four harnesses occupy only three backend names in
# this template: Qwen Code runs on ``acp`` (QWEN.md), and associate/Pi runs on
# ``colleague`` (AGENTS.override.md as context plus .pi/SYSTEM.md as the system
# prompt). This is a *recognition* table — it answers "does some harness on
# this backend read this file?", which is what tooling needs when it walks a
# checkout and attributes files to backends.
#
# It is deliberately NOT the health check. See ``_RESIDENT_PROMPT`` below.
#
# This mirrors ``backends[*].prompt`` in
# ``.claude/skills/agent-config/data/backend-fingerprints.yaml``;
# ``tests/test_harness_registries.py`` asserts the two never drift apart.
_PROMPT_FILE = {
    "claude": ("CLAUDE.md",),
    "colleague": ("AGENTS.colleague.md", "AGENTS.override.md", ".pi/SYSTEM.md"),
    "acp": (_AGENTS_MD, "QWEN.md"),
    "codex": (_AGENTS_MD,),
    "copilot": (_AGENTS_MD,),
    "gemini": ("GEMINI.md",),
}

# backend → the ONE prompt file the Culture daemon reads for a resident on
# that backend. This is what ``prompt_file_present`` requires.
#
# The distinction from ``_PROMPT_FILE`` is load-bearing. Several files are
# recognized under ``colleague`` and ``acp`` because *interactive harnesses*
# ride those backend names — Pi reads ``AGENTS.override.md`` + ``.pi/SYSTEM.md``
# and Qwen Code reads ``QWEN.md`` — but the mesh resident reads neither. A
# clone declaring ``backend: colleague`` with only Pi's files on disk has no
# resident prompt at all, and treating the harness files as interchangeable
# alternatives let exactly that pass as healthy.
_RESIDENT_PROMPT = {
    "claude": "CLAUDE.md",
    "colleague": "AGENTS.colleague.md",
    "acp": _AGENTS_MD,
    "codex": _AGENTS_MD,
    "copilot": _AGENTS_MD,
    "gemini": "GEMINI.md",
}


def _diagnose() -> dict[str, object]:
    cfg = find_culture_yaml()
    if cfg is None:
        check = {
            "id": "source_checkout",
            "passed": True,
            "severity": "info",
            "message": "no culture.yaml found alongside the package; identity checks skipped",
            "remediation": "",
        }
        return {"healthy": True, "checks": [check]}

    root = cfg.parent
    fields = read_agent_fields()
    backend = fields["backend"]
    checks: list[dict[str, object]] = []

    # 1. backend-consistency: the RESIDENT prompt file for the declared
    #    backend exists. Other harness prompt files recognized under the same
    #    backend name are reported separately (check 2) and never substituted
    #    here — the mesh daemon does not read them.
    resident = _RESIDENT_PROMPT.get(backend)
    if resident is None:
        checks.append(
            {
                "id": "backend_consistency",
                "passed": False,
                "severity": "error",
                "message": f"unknown backend '{backend}' in culture.yaml",
                "remediation": f"set backend to one of: {', '.join(sorted(_RESIDENT_PROMPT))}",
            }
        )
    else:
        present = (root / resident).is_file()
        checks.append(
            {
                "id": "prompt_file_present",
                "passed": present,
                "severity": "error",
                "message": (
                    f"backend '{backend}' reads {resident} as its resident prompt — "
                    + ("present" if present else "missing")
                ),
                "remediation": "" if present else f"create {resident} at the repo root",
            }
        )

        # 2. harness-prompts: report the other recognized prompt files on
        #    disk for this backend. Informational — an interactively available
        #    harness is not a health requirement, and its absence is not a
        #    failure — but it must never stand in for the resident prompt.
        others = [
            name
            for name in _PROMPT_FILE.get(backend, ())
            if name != resident and (root / name).is_file()
        ]
        checks.append(
            {
                "id": "harness_prompts",
                "passed": True,
                "severity": "info",
                "message": (
                    "other harness prompt files on this backend: "
                    + (", ".join(others) if others else "none")
                    + " (not substitutes for the resident prompt)"
                ),
                "remediation": "",
            }
        )

    # 3. skills-present: the vendored skill kit is on disk.
    skills_dir = root / ".claude" / "skills"
    has_skills = skills_dir.is_dir() and any(skills_dir.iterdir())
    checks.append(
        {
            "id": "skills_present",
            "passed": has_skills,
            "severity": "warning",
            "message": (
                ".claude/skills/ vendored" if has_skills else ".claude/skills/ missing or empty"
            ),
            "remediation": (
                "" if has_skills else "vendor the skill kit (see docs/skill-sources.md)"
            ),
        }
    )

    healthy = all(c["passed"] for c in checks)
    return {"healthy": healthy, "checks": checks}


def _check(cid: str, status: str, message: str, remediation: str = "") -> dict[str, Any]:
    severity = {"fail": "error", "warn": "warning"}.get(status, "info")
    return {
        "id": cid,
        "passed": status != "fail",
        "severity": severity,
        "status": status,
        "message": message,
        "remediation": remediation if status in ("warn", "fail") else "",
    }


_CONFIGURED = ("api_key", "mongo_uri", "webhook_secret", "gateway_api_key")


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _gateway_check(rt: Any) -> dict[str, Any]:
    url_env = config.ENV_VARS["gateway_url"]
    try:
        caps = rt.gateway_probe()
    except CliError as exc:
        return _check(
            "gateway_reachability",
            "fail",
            f"gateway at {_runtime.capabilities_url(rt.settings.gateway_url)}: {exc.message}",
            f"start the lobes gateway or point {url_env} at it",
        )
    if EMBED_MODEL not in str(caps):
        return _check(
            "gateway_reachability",
            "warn",
            f"gateway reachable but does not advertise {EMBED_MODEL}",
            f"deploy the {EMBED_MODEL} embedding lane on the gateway",
        )
    if not rt.settings.gateway_api_key:
        key_env = config.ENV_VARS["gateway_api_key"]
        return _check(
            "gateway_reachability",
            "warn",
            f"gateway reachable; {key_env} is not set, so enrichment calls will be refused",
            f"set {key_env}",
        )
    return _check("gateway_reachability", "ok", f"gateway reachable; {EMBED_MODEL} advertised")


def _index_check(collection: Any) -> dict[str, Any]:
    try:
        vindex.check(collection, EMBED_MODEL, EMBED_DIMS)
    except CliError as exc:
        return _check("vector_index_model", "fail", exc.message, exc.remediation)
    except Exception as exc:
        return _check(
            "vector_index_model",
            "warn",
            f"search indexes could not be listed ({type(exc).__name__})",
            "use the atlas-local deployment (docs/operations/pocketsync-mongo.md)",
        )
    return _check(
        "vector_index_model", "ok", f"index and stored vectors use {EMBED_MODEL}/{EMBED_DIMS}"
    )


def _webhook_check(rt: Any, state: Any, now: datetime) -> dict[str, Any]:
    if not rt.settings.webhook_secret:
        return _check("webhook_freshness", "info", "serve not configured (no webhook secret)")
    last = state.get_last_webhook_at()
    hint = (
        "check the tunnel/Access bypass to 127.0.0.1 and that the Pocket webhook secret "
        "matches POCKETSYNC_WEBHOOK_SECRET; ignore if nothing was recorded lately"
    )
    when = _parse_ts(last)
    if when is None:
        return _check("webhook_freshness", "warn", "no verified webhook received yet", hint)
    age_h = (now - when).total_seconds() / 3600
    if age_h > WEBHOOK_STALE_HOURS:
        return _check(
            "webhook_freshness",
            "warn",
            f"last verified webhook {age_h:.0f} hours ago (threshold {WEBHOOK_STALE_HOURS})",
            hint,
        )
    return _check("webhook_freshness", "ok", f"last verified webhook at {last}")


def _health_checks(state: Any, collection: Any, now: datetime) -> list[dict[str, Any]]:
    from pocketsync.cli._commands.status import enrichment_backlog

    out = []
    missed = int(state.counters().get(COUNTER_WEBHOOK_MISSED, 0))
    out.append(
        _check(
            "webhook_missed",
            "warn" if missed else "ok",
            f"{missed} recording(s) found by the reconciler that no webhook delivered (cumulative)",
            "the push path dropped events: check the tunnel, Access bypass and webhook secret",
        )
    )
    backlog = enrichment_backlog(collection)
    out.append(
        _check(
            "enrichment_backlog",
            "warn" if backlog > BACKLOG_WARN else "ok",
            f"{backlog} recording(s) pending enrichment",
            "check the gateway (gateway_reachability) and that serve is running",
        )
    )
    cutoff = now - timedelta(hours=RECENT_ERROR_HOURS)
    recent = [e for e in state.errors() if (_parse_ts(e.get("at")) or now) >= cutoff]
    out.append(
        _check(
            "recent_errors",
            "warn" if recent else "ok",
            f"{len(recent)} error(s) in the last {RECENT_ERROR_HOURS}h"
            + (f"; latest: {recent[-1].get('message')}" if recent else ""),
            "run 'pocketsync status' for details",
        )
    )
    return out


def _skipped(ids: tuple[str, ...], why: str) -> list[dict[str, Any]]:
    return [_check(i, "warn", f"skipped: {why}", "fix mongo_auth first") for i in ids]


_MONGO_DEPENDENT = (
    "vector_index_model",
    "webhook_freshness",
    "webhook_missed",
    "enrichment_backlog",
    "recent_errors",
)


def runtime_checks() -> list[dict[str, Any]]:
    """Gateway, Mongo, index/model and webhook-health checks for the configured runtime."""
    try:
        rt = _runtime.get()
    except CliError as exc:
        return [_check("config", "fail", exc.message, exc.remediation)]
    s = rt.settings
    if not any(getattr(s, name) for name in _CONFIGURED):
        return [
            _check(
                "runtime_config",
                "info",
                "nothing configured (no Pocket key, Mongo URI, webhook secret or gateway key); "
                "runtime checks skipped",
            )
        ]
    now = datetime.now(timezone.utc)
    checks = [_gateway_check(rt)]
    if not s.mongo_uri:
        env = config.ENV_VARS["mongo_uri"]
        checks.append(_check("mongo_auth", "warn", f"{env} is not set", f"set {env}"))
        return checks + _skipped(_MONGO_DEPENDENT, "no Mongo URI")
    try:
        rt.mongo_ping()
    except CliError as exc:
        checks.append(_check("mongo_auth", "fail", exc.message, exc.remediation))
        return checks + _skipped(_MONGO_DEPENDENT, "Mongo unavailable")
    except Exception as exc:  # pymongo errors; never echo the URI
        checks.append(
            _check(
                "mongo_auth",
                "fail",
                f"Mongo ping failed ({type(exc).__name__})",
                f"check {config.ENV_VARS['mongo_uri']} (host, user, password) "
                "and that mongod is up",
            )
        )
        return checks + _skipped(_MONGO_DEPENDENT, "Mongo unavailable")
    checks.append(_check("mongo_auth", "ok", "Mongo ping succeeded with the configured URI"))
    collection = rt.store_sink().collection
    state = rt.state()
    checks.append(_index_check(collection))
    checks.append(_webhook_check(rt, state, now))
    checks.extend(_health_checks(state, collection, now))
    return checks


def _status_of(check: dict[str, object]) -> str:
    if check.get("status"):
        return str(check["status"])
    if not check["passed"]:
        return "fail"
    return "info" if check["severity"] == "info" else "ok"


def diagnose_all() -> dict[str, object]:
    """Identity checks (:func:`_diagnose`) plus the runtime checks, with a ``status`` each."""
    report = _diagnose()
    checks = list(report["checks"]) + runtime_checks()  # type: ignore[arg-type]
    for check in checks:
        check["status"] = _status_of(check)
    return {"healthy": all(c["passed"] for c in checks), "checks": checks}


_MARKS = {"ok": "ok", "info": "info", "warn": "warn", "fail": "FAIL"}


def cmd_doctor(args: argparse.Namespace) -> int:
    report = diagnose_all()
    json_mode = bool(getattr(args, "json", False))
    if json_mode:
        emit_result(report, json_mode=True)
    else:
        status = "healthy" if report["healthy"] else "unhealthy"
        lines = [f"pocketsync doctor: {status}", ""]
        for check in report["checks"]:
            mark = _MARKS.get(check["status"], check["status"])
            lines.append(f"[{mark}] {check['id']}: {check['message']}")
            if check["status"] in ("warn", "fail") and check["remediation"]:
                lines.append(f"  hint: {check['remediation']}")
        emit_result("\n".join(lines), json_mode=False)
    return 0 if report["healthy"] else 1


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "doctor",
        help="Check agent identity plus runtime health (Mongo, gateway, vector index, webhooks).",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_doctor)
