"""Re-exec under ``grant`` when the secret settings live there, not in the environment.

The deploy keeps every pocketsync secret as a *hidden* grant secret named exactly like its
environment variable (``POCKETSYNC_MONGO_URI`` and so on). Hidden secrets cannot be read
(``grant get`` refuses them), only injected into a child process by ``grant run --inject``.
So a bare ``pocketsync status`` in the operator's shell would fail on a missing setting.
:func:`maybe_reexec` closes that gap. For a verb that needs runtime settings, when some secret
variables are unset and grant holds secrets of those names, it replaces the process with
``grant run --inject VAR=VAR ... -- python -m pocketsync <argv>``.

It never loops (``POCKETSYNC_VIA_GRANT`` marks the child), and it never overrides a value that
is already set. It stays out of the way when grant is absent or holds none of the names, and
``POCKETSYNC_NO_GRANT=1`` turns it off. The values never pass through pocketsync, argv or disk.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 - runs the operator's own grant binary, fixed argv
import sys
from collections.abc import Callable, Mapping

SECRET_ENV = (
    "POCKETSYNC_API_KEY",
    "POCKETSYNC_WEBHOOK_SECRET",
    "POCKETSYNC_GATEWAY_API_KEY",
    "POCKETSYNC_MONGO_URI",
)
REEXEC_MARK = "POCKETSYNC_VIA_GRANT"
OPT_OUT = "POCKETSYNC_NO_GRANT"
#: Verbs that build a runtime (and so may need secrets). Introspection verbs never do.
RUNTIME_VERBS = frozenset({"auth", "list", "sync", "watch", "serve", "status", "reembed", "doctor"})


def _verb(argv: list[str]) -> str | None:
    return next((a for a in argv if not a.startswith("-")), None)


def grant_names(grant: str, run: Callable[..., object] = subprocess.run) -> set[str]:
    """Secret names in the operator's grant store (names only; empty on any failure)."""
    try:
        proc = run(  # nosec B603 - fixed argv, no shell
            [grant, "list", "--json"], capture_output=True, text=True, timeout=10
        )
        data = json.loads(getattr(proc, "stdout", "") or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return set()
    if getattr(proc, "returncode", 1) != 0 or not data.get("ok"):
        return set()
    return {str(n) for n in data.get("names") or []}


def maybe_reexec(
    argv: list[str],
    *,
    env: Mapping[str, str] = os.environ,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., object] = subprocess.run,
    execvpe: Callable[..., object] = os.execvpe,
) -> None:
    """Replace the process with ``grant run --inject ...`` when that supplies missing secrets.

    Returns (doing nothing) whenever re-exec cannot or should not help.
    """
    if env.get(REEXEC_MARK) or env.get(OPT_OUT) or _verb(argv) not in RUNTIME_VERBS:
        return None
    missing = [v for v in SECRET_ENV if not env.get(v)]
    if not missing:
        return None
    grant = which("grant")
    if not grant:
        return None
    held = grant_names(grant, run)
    inject = [v for v in missing if v in held]
    if not inject:
        return None
    cmd = [grant, "run"]
    for var in inject:
        cmd += ["--inject", f"{var}={var}"]
    cmd += ["--", sys.executable, "-m", "pocketsync", *argv]
    execvpe(grant, cmd, {**env, REEXEC_MARK: "1"})
    return None
