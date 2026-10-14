"""Settings and secrets loader (stdlib only; TOML via ``tomllib``).

Precedence: environment variables, then the optional TOML file (path given
explicitly or via ``POCKETSYNC_CONFIG``), then defaults. Secret fields are
excluded from ``repr``/``str`` and never placed in error messages.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping

from pocketsync.cli._errors import EXIT_ENV_ERROR, CliError

DEFAULT_GATEWAY_URL = "http://localhost:8001/v1"
DEFAULT_FILES_ROOT = "~/.local/share/pocketsync/files"

# setting name -> environment variable: always POCKETSYNC_<NAME>
_SETTING_NAMES = (
    "api_key",
    "webhook_secret",
    "gateway_api_key",
    "mongo_uri",
    "files_root",
    "gateway_url",
    "webhook_port",
    "reconcile_interval",
    "enrichment_concurrency",
    "taxonomy_path",
)
ENV_VARS = {name: f"POCKETSYNC_{name.upper()}" for name in _SETTING_NAMES}
CONFIG_PATH_ENV = "POCKETSYNC_CONFIG"
_INT_FIELDS = {"webhook_port", "reconcile_interval", "enrichment_concurrency"}


@dataclass(frozen=True)
class Settings:
    """Resolved settings. Secrets (``repr=False``) are api_key, webhook_secret,
    gateway_api_key and mongo_uri (which may embed a password)."""

    api_key: str | None = field(default=None, repr=False)
    webhook_secret: str | None = field(default=None, repr=False)
    gateway_api_key: str | None = field(default=None, repr=False)
    mongo_uri: str | None = field(default=None, repr=False)
    files_root: str = DEFAULT_FILES_ROOT
    gateway_url: str = DEFAULT_GATEWAY_URL
    webhook_port: int = 8787
    reconcile_interval: int = 300
    enrichment_concurrency: int = 2
    taxonomy_path: str | None = None

    def __str__(self) -> str:
        return repr(self)

    def require(self, *names: str) -> Settings:
        """Raise ``CliError(2)`` naming the env vars of any unset ``names``; else return self."""
        known = {f.name for f in fields(self)}
        unknown = [n for n in names if n not in known]
        if unknown:
            raise ValueError(f"unknown setting(s): {', '.join(unknown)}")
        missing = [n for n in names if getattr(self, n) in (None, "")]
        if missing:
            envs = ", ".join(ENV_VARS[n] for n in missing)
            raise CliError(
                EXIT_ENV_ERROR,
                f"missing required setting(s): {envs}",
                f"Set {envs} in the environment or in the TOML config "
                f"(path via {CONFIG_PATH_ENV}).",
            )
        return self


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except OSError as exc:
        raise CliError(
            EXIT_ENV_ERROR,
            f"cannot read config file {path}: {exc.strerror or exc}",
            f"Check the path (set via {CONFIG_PATH_ENV} or --config) exists and is readable.",
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise CliError(
            EXIT_ENV_ERROR,
            f"invalid TOML in {path}: {exc}",
            "Fix the TOML syntax in the config file.",
        ) from exc


def load(
    env: Mapping[str, str] | None = None, config_path: str | os.PathLike[str] | None = None
) -> Settings:
    """Build :class:`Settings` from ``env`` (default ``os.environ``) and an optional TOML file."""
    env = os.environ if env is None else env
    path = config_path or env.get(CONFIG_PATH_ENV)
    data = _read_toml(Path(path).expanduser()) if path else {}
    values: dict[str, Any] = {}
    for name, var in ENV_VARS.items():
        raw = env.get(var)
        if raw is None or raw == "":
            raw = data.get(name)
        if raw is None or raw == "":
            continue
        if name in _INT_FIELDS:
            try:
                raw = int(raw)
            except (TypeError, ValueError) as exc:
                raise CliError(
                    EXIT_ENV_ERROR,
                    f"setting {name} ({var}) must be an integer",
                    f"Set {var} (or '{name}' in the config file) to a whole number.",
                ) from exc
        values[name] = raw
    return Settings(**values)
