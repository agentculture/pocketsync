"""Re-exec under ``grant`` when the secret settings live there, not in the environment.

The deploy keeps every pocketsync secret as a *hidden* grant secret named exactly like its
environment variable (``POCKETSYNC_MONGO_URI`` and so on). Hidden secrets cannot be read
(``grant get`` refuses them), only injected into a child process by ``grant run --inject``.
So a bare ``pocketsync status`` in the operator's shell would fail on a missing setting.
:func:`maybe_reexec` closes that gap. For a verb that needs runtime settings, when some secret
variables are unset and grant holds secrets of those names, it replaces the process with
``grant run --inject VAR=VAR ... -- python -m pocketsync <argv>``.

It never loops (``POCKETSYNC_VIA_GRANT`` marks the child), and it never overrides a non-empty
value (an empty variable counts as unset, exactly as the settings loader treats it). It
stays out of the way when grant is absent or holds none of the names, and
``POCKETSYNC_NO_GRANT=1`` turns it off. The values never pass through pocketsync, argv or disk.

Once exec'd, a failure inside grant itself (a locked store, a refused inject) is reported by
grant in its own format and exit code, not pocketsync's ``--json`` error contract; if the exec
itself fails, pocketsync carries on and reports the missing setting as usual.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 - runs the operator's own grant binary, fixed argv
import sys
from collections.abc import Callable, Mapping

from pocketsync.config import ENV_VARS, SECRET_SETTINGS

SECRET_ENV = tuple(ENV_VARS[name] for name in SECRET_SETTINGS)
REEXEC_MARK = "POCKETSYNC_VIA_GRANT"
OPT_OUT = "POCKETSYNC_NO_GRANT"
#: Verbs that build a runtime (and so may need secrets). Introspection verbs never do.
RUNTIME_VERBS = frozenset(
    {
        "auth",
        "list",
        "sync",
        "watch",
        "serve",
        "status",
        "reembed",
        "doctor",
        "enrich",
        "search",
        "get",
        "transcript",
        "recordings",
        "actions",
        "mcp",
        "web",
    }
)


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
    if getattr(proc, "returncode", 1) != 0 or not isinstance(data, dict) or not data.get("ok"):
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
    if "-h" in argv or "--help" in argv:
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
    try:
        execvpe(grant, cmd, {**env, REEXEC_MARK: "1"})
    except OSError:
        return None  # grant vanished or is not executable: carry on and report normally
    return None
