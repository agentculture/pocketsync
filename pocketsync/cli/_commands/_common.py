"""Small helpers shared by the operational verbs (flags, arg validation, text rendering)."""

from __future__ import annotations

import argparse
from datetime import date
from typing import Any

from pocketsync.cli._errors import EXIT_USER_ERROR, CliError


def add_json(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")


def add_apply(p: argparse.ArgumentParser, what: str) -> None:
    p.add_argument(
        "--apply",
        action="store_true",
        help=f"Commit {what}. Without it the verb is a dry run and writes nothing.",
    )


def json_mode(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def parse_day(value: str | None, flag: str = "--since") -> str | None:
    """Validate a ``YYYY-MM-DD`` (or ISO timestamp) value; return its ``YYYY-MM-DD`` day."""
    if value is None:
        return None
    try:
        return date.fromisoformat(value.strip()[:10]).isoformat()
    except ValueError as exc:
        raise CliError(
            EXIT_USER_ERROR,
            f"invalid {flag} date: {value!r}",
            "Use YYYY-MM-DD (for example 2026-10-01).",
        ) from exc


def positive(value: float, flag: str) -> float:
    if value <= 0:
        raise CliError(EXIT_USER_ERROR, f"{flag} must be > 0", f"Pass a positive {flag}.")
    return value


def kv_lines(pairs: list[tuple[str, Any]]) -> str:
    width = max(len(k) for k, _ in pairs)
    return "\n".join(f"{k.ljust(width)}  {'-' if v is None else v}" for k, v in pairs)
