"""Static checks on the systemd units and runbook for pocketsync.culture.dev (t18)."""

from __future__ import annotations

import configparser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVE = ROOT / "deploy" / "systemd" / "pocketsync-serve.service"
TUNNEL = ROOT / "deploy" / "cloudflared" / "cloudflared-pocketsync.service"
RUNBOOK = ROOT / "docs" / "operations" / "pocketsync-culture-dev.md"


def _unit(path: Path) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str  # keep key case
    text = path.read_text().replace("\\\n", " ")
    cp.read_string(text)
    return cp


def test_serve_unit_restarts_and_injects_all_secrets_via_grant():
    u = _unit(SERVE)
    svc = u["Service"]
    assert svc["Restart"] == "on-failure"
    exec_start = svc["ExecStart"]
    assert "grant run" in exec_start and exec_start.rstrip().endswith("pocketsync serve")
    for var in (
        "POCKETSYNC_API_KEY",
        "POCKETSYNC_WEBHOOK_SECRET",
        "POCKETSYNC_GATEWAY_API_KEY",
        "POCKETSYNC_MONGO_URI",
    ):
        assert f"--inject {var}=@" in exec_start  # placeholder, never a value
    assert u["Install"]["WantedBy"] == "default.target"


def test_units_hold_no_secret_values():
    for path in (SERVE, TUNNEL):
        text = path.read_text()
        assert "pk_" not in text
        assert "mongodb://" not in text
        assert "TUNNEL_TOKEN=@TUNNEL_TOKEN_SECRET@" in text or path is SERVE


def test_tunnel_unit_mirrors_culture_rules_connector():
    svc = _unit(TUNNEL)["Service"]
    assert "cloudflared tunnel --no-autoupdate run" in svc["ExecStart"]
    assert svc["Restart"] == "on-failure"


def test_runbook_keeps_access_on_and_bypasses_only_the_exact_path():
    text = RUNBOOK.read_text()
    assert "pocketsync.culture.dev/hooks/pocket" in text
    assert "--no-access" in text and "**Do not** use `--no-access`" in text
    assert "Never widen it" in text
    assert "127.0.0.1:<port>" in text
