"""Offline tests for pocketsync.config (synthetic secrets only)."""

from __future__ import annotations

import pytest

from pocketsync.cli._errors import CliError
from pocketsync.config import Settings, load

SECRETS = {
    "POCKETSYNC_API_KEY": "synthetic-api-value",
    "POCKETSYNC_WEBHOOK_SECRET": "synthetic-hook-value",
    "POCKETSYNC_GATEWAY_API_KEY": "synthetic-gw-value",
    "POCKETSYNC_MONGO_URI": "mongodb://user:synthpw@localhost:27017/db",
}


def test_defaults():
    s = load(env={})
    assert s.gateway_url == "http://localhost:8001/v1"
    assert s.files_root.endswith(".local/share/pocketsync/files")
    assert s.reconcile_interval == 300
    assert s.enrichment_concurrency == 2
    assert s.taxonomy_path is None
    assert s.api_key is None


def test_env_loading():
    env = dict(SECRETS, POCKETSYNC_FILES_ROOT="/tmp/x")
    s = load(env=env)
    assert s.api_key == "synthetic-api-value"
    assert s.webhook_secret == "synthetic-hook-value"
    assert s.gateway_api_key == "synthetic-gw-value"
    assert s.mongo_uri.startswith("mongodb://")
    assert s.files_root == "/tmp/x"


def test_toml_file_and_env_precedence(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('api_key = "from-file"\nwebhook_port = 9999\nreconcile_interval = 60\n')
    s = load(env={}, config_path=p)
    assert s.api_key == "from-file"
    assert s.webhook_port == 9999
    assert s.reconcile_interval == 60
    s = load(env={"POCKETSYNC_API_KEY": "from-env"}, config_path=p)
    assert s.api_key == "from-env"


def test_config_path_from_env(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('taxonomy_path = "/t.yaml"\n')
    s = load(env={"POCKETSYNC_CONFIG": str(p)})
    assert s.taxonomy_path == "/t.yaml"


def test_bad_config_file_raises_cli_error(tmp_path):
    p = tmp_path / "bad.toml"
    p.write_text("not = = toml")
    with pytest.raises(CliError) as e:
        load(env={}, config_path=p)
    assert e.value.code == 2
    assert e.value.remediation
    with pytest.raises(CliError):
        load(env={}, config_path=tmp_path / "missing.toml")


def test_require_missing_raises_code_2_with_hint():
    s = load(env={})
    with pytest.raises(CliError) as e:
        s.require("api_key", "mongo_uri")
    assert e.value.code == 2
    assert "POCKETSYNC_API_KEY" in e.value.message
    assert "POCKETSYNC_MONGO_URI" in e.value.message
    assert "POCKETSYNC_API_KEY" in e.value.remediation


def test_require_ok_returns_self():
    s = load(env=SECRETS)
    assert s.require("api_key") is s


def test_require_unknown_name():
    s = load(env={})
    with pytest.raises(ValueError):
        s.require("nope")


def test_secrets_never_in_repr_or_str():
    s = load(env=SECRETS)
    for text in (repr(s), str(s), f"{s}"):
        for v in SECRETS.values():
            assert v not in text
        assert "synthpw" not in text
    assert isinstance(s, Settings)


def test_secret_not_in_error_message():
    s = load(env={"POCKETSYNC_API_KEY": "synthetic-api-value"})
    with pytest.raises(CliError) as e:
        s.require("api_key", "webhook_secret")
    assert "synthetic-api-value" not in e.value.message + e.value.remediation


def test_talk_settings_defaults():
    s = load(env={})
    assert s.chat_model == "unsloth/Qwen3.8-27B-NVFP4"
    assert s.web_port == 8788
    assert s.access_team_domain is None
    assert s.access_aud is None
    assert s.site_origin == "https://pocketchat.culture.dev"


def test_talk_settings_env_override():
    s = load(
        env={
            "POCKETSYNC_CHAT_MODEL": "m/x",
            "POCKETSYNC_WEB_PORT": "9000",
            "POCKETSYNC_ACCESS_TEAM_DOMAIN": "team.example.test",
            "POCKETSYNC_ACCESS_AUD": "aud123",
            "POCKETSYNC_SITE_ORIGIN": "http://localhost:1",
        }
    )
    assert (s.chat_model, s.web_port) == ("m/x", 9000)
    assert s.access_team_domain == "team.example.test"
    assert s.access_aud == "aud123"
    assert s.site_origin == "http://localhost:1"


def test_web_port_must_be_int():
    with pytest.raises(CliError):
        load(env={"POCKETSYNC_WEB_PORT": "abc"})


def test_secret_settings_unchanged():
    from pocketsync.config import SECRET_SETTINGS

    assert SECRET_SETTINGS == ("api_key", "webhook_secret", "gateway_api_key", "mongo_uri")
