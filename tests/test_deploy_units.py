"""Static checks on the systemd units and runbook for pocketsync.culture.dev (t18)."""

from __future__ import annotations

import configparser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVE = ROOT / "deploy" / "systemd" / "pocketsync-serve.service"
WEB = ROOT / "deploy" / "systemd" / "pocketsync-web.service"
TUNNEL = ROOT / "deploy" / "cloudflared" / "cloudflared-pocketsync.service"
RUNBOOK = ROOT / "docs" / "operations" / "pocketsync-culture-dev.md"
CHAT_RUNBOOK = ROOT / "docs" / "operations" / "pocketchat-culture-dev.md"


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
    assert "grant run" in exec_start
    assert exec_start.rstrip().endswith("pocketsync serve")
    for var in (
        "POCKETSYNC_API_KEY",
        "POCKETSYNC_WEBHOOK_SECRET",
        "POCKETSYNC_GATEWAY_API_KEY",
        "POCKETSYNC_MONGO_URI",
    ):
        assert f"--inject {var}=@" in exec_start  # placeholder, never a value
    assert u["Install"]["WantedBy"] == "default.target"


def test_units_hold_no_secret_values():
    for path in (SERVE, WEB, TUNNEL):
        text = path.read_text()
        assert "pk_" not in text
        assert "mongodb://" not in text
        assert "TUNNEL_TOKEN=@TUNNEL_TOKEN_SECRET@" in text or path in (SERVE, WEB)


def test_tunnel_unit_mirrors_culture_rules_connector():
    svc = _unit(TUNNEL)["Service"]
    assert "cloudflared tunnel --no-autoupdate run" in svc["ExecStart"]
    assert svc["Restart"] == "on-failure"


def test_runbook_keeps_access_on_and_bypasses_only_the_exact_path():
    text = RUNBOOK.read_text()
    assert "pocketsync.culture.dev/hooks/pocket" in text
    assert "--no-access" in text
    assert "**Do not** use `--no-access`" in text
    assert "Never widen it" in text
    assert "127.0.0.1:<port>" in text


def test_web_unit_injects_only_its_secrets_and_sets_access_env():
    svc = _unit(WEB)["Service"]
    assert svc["Restart"] == "on-failure"
    exec_start = svc["ExecStart"]
    assert "grant run" in exec_start
    assert exec_start.rstrip().endswith("pocketsync web")
    for var in ("POCKETSYNC_GATEWAY_API_KEY", "POCKETSYNC_MONGO_URI"):
        assert f"--inject {var}=@" in exec_start  # placeholder, never a value
    # the web verb never talks to Pocket: it gets neither the pk_ key nor the webhook secret
    assert "POCKETSYNC_API_KEY" not in exec_start
    assert "POCKETSYNC_WEBHOOK_SECRET" not in exec_start
    text = WEB.read_text()
    assert "Environment=POCKETSYNC_WEB_PORT=@WEB_PORT@" in text
    assert "Environment=POCKETSYNC_ACCESS_TEAM_DOMAIN=@ACCESS_TEAM_DOMAIN@" in text
    assert "Environment=POCKETSYNC_ACCESS_AUD=@ACCESS_AUD@" in text
    assert _unit(WEB)["Install"]["WantedBy"] == "default.target"


def test_chat_runbook_creates_access_first_and_never_bypasses():
    text = CHAT_RUNBOOK.read_text()
    assert "pocketchat.culture.dev" in text
    assert "pocketsync-culture-dev" in text
    assert "http://127.0.0.1:8788" in text
    assert "--reinstall" in text
    assert "NO Bypass" in text
    assert text.index("Access application") < text.index("public hostname")
    assert text.index("public hostname") < text.index("pocketsync-web.service")
    assert "Rollback" in text
