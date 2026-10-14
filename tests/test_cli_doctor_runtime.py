"""`pocketsync doctor` runtime checks: gateway, mongo auth, index/model, webhook health."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from pocketsync import vindex
from pocketsync.cli import _runtime, main
from pocketsync.cli._commands import doctor as doctor_cmd
from pocketsync.cli._errors import CliError
from pocketsync.state import COUNTER_WEBHOOK_MISSED
from tests.cli_fakes import FakeRuntime, install

RUNTIME_IDS = {
    "mongo_auth",
    "gateway_reachability",
    "vector_index_model",
    "webhook_freshness",
    "webhook_missed",
    "enrichment_backlog",
    "recent_errors",
}


def _checks(capsys):
    report = json.loads(capsys.readouterr().out)
    return report, {c["id"]: c for c in report["checks"]}


@pytest.fixture
def rt(tmp_path, monkeypatch):
    rt = install(monkeypatch, FakeRuntime(tmp_path, POCKETSYNC_GATEWAY_API_KEY="gw_synthetic"))
    monkeypatch.setattr(vindex, "_existing", lambda col: {})  # mongomock has no search indexes
    return rt


def _fresh(rt, hours_ago=1.0):
    when = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    rt.counting_state.inner.set_last_webhook_at(when.isoformat())


def test_all_green(rt, capsys):
    _fresh(rt)
    assert main(["doctor", "--json"]) == 0
    report, checks = _checks(capsys)
    assert report["healthy"] is True
    assert RUNTIME_IDS <= set(checks)
    for cid in RUNTIME_IDS:
        assert checks[cid]["status"] == "ok", checks[cid]
    # identity checks are kept
    assert {"prompt_file_present", "skills_present"} <= set(checks)


def test_every_check_has_contract_keys(rt, capsys):
    rt.ping_error = RuntimeError("Authentication failed.")
    main(["doctor", "--json"])
    _, checks = _checks(capsys)
    for c in checks.values():
        assert {"id", "passed", "severity", "status", "message", "remediation"} <= set(c)
        assert c["status"] in {"ok", "warn", "fail", "info"}
        assert c["passed"] is (c["status"] != "fail")
        if c["status"] in {"warn", "fail"}:
            assert c["remediation"], c


def test_mongo_auth_failure_fails_and_skips_dependents(rt, capsys):
    rt.ping_error = RuntimeError("Authentication failed.")
    assert main(["doctor", "--json"]) == 1
    report, checks = _checks(capsys)
    assert report["healthy"] is False
    assert checks["mongo_auth"]["status"] == "fail"
    assert "POCKETSYNC_MONGO_URI" in checks["mongo_auth"]["remediation"]
    for cid in ("vector_index_model", "webhook_freshness", "enrichment_backlog"):
        assert checks[cid]["status"] == "warn"
    assert "mongodb://" not in json.dumps(report)  # URI never echoed


def test_gateway_unreachable_fails(rt, capsys):
    rt.probe_result = CliError(2, "gateway unreachable: connection refused", "")
    _fresh(rt)
    assert main(["doctor", "--json"]) == 1
    _, checks = _checks(capsys)
    assert checks["gateway_reachability"]["status"] == "fail"
    assert "POCKETSYNC_GATEWAY_URL" in checks["gateway_reachability"]["remediation"]


def test_gateway_key_unset_is_warn(tmp_path, monkeypatch, capsys):
    rt = install(monkeypatch, FakeRuntime(tmp_path))
    monkeypatch.setattr(vindex, "_existing", lambda col: {})
    _fresh(rt)
    assert main(["doctor", "--json"]) == 0
    _, checks = _checks(capsys)
    assert checks["gateway_reachability"]["status"] == "warn"
    assert "POCKETSYNC_GATEWAY_API_KEY" in checks["gateway_reachability"]["remediation"]


def test_gateway_without_embed_model_warns(rt, capsys):
    rt.probe_result = {"embed": {"other-model": {}}}
    _fresh(rt)
    main(["doctor", "--json"])
    _, checks = _checks(capsys)
    assert checks["gateway_reachability"]["status"] == "warn"


def test_index_model_mismatch_fails_with_reembed_hint(rt, capsys):
    rt.db[vindex.META_COLLECTION].insert_one({"_id": vindex.META_ID, "model": "old", "dims": 512})
    _fresh(rt)
    assert main(["doctor", "--json"]) == 1
    _, checks = _checks(capsys)
    assert checks["vector_index_model"]["status"] == "fail"
    assert "reembed --apply" in checks["vector_index_model"]["remediation"]


def test_index_not_inspectable_warns(rt, monkeypatch, capsys):
    def boom(col):
        raise TypeError("no list_search_indexes")

    monkeypatch.setattr(vindex, "_existing", boom)
    _fresh(rt)
    assert main(["doctor", "--json"]) == 0
    _, checks = _checks(capsys)
    assert checks["vector_index_model"]["status"] == "warn"


def test_stale_and_missing_webhook_warn(rt, capsys):
    main(["doctor", "--json"])
    _, checks = _checks(capsys)
    assert checks["webhook_freshness"]["status"] == "warn"  # never received
    _fresh(rt, hours_ago=doctor_cmd.WEBHOOK_STALE_HOURS + 1)
    main(["doctor", "--json"])
    _, checks = _checks(capsys)
    assert checks["webhook_freshness"]["status"] == "warn"
    assert "hours" in checks["webhook_freshness"]["message"]


def test_webhook_freshness_info_when_serve_not_configured(tmp_path, monkeypatch, capsys):
    install(monkeypatch, FakeRuntime(tmp_path, POCKETSYNC_WEBHOOK_SECRET=""))
    monkeypatch.setattr(vindex, "_existing", lambda col: {})
    main(["doctor", "--json"])
    _, checks = _checks(capsys)
    assert checks["webhook_freshness"]["status"] == "info"


def test_webhook_missed_backlog_and_errors_warn(rt, capsys, monkeypatch):
    _fresh(rt)
    st = rt.counting_state.inner
    st.increment(COUNTER_WEBHOOK_MISSED, 3)
    st.record_error("reconcile", "boom")
    monkeypatch.setattr(doctor_cmd, "BACKLOG_WARN", 0)
    rt.db["recordings"].insert_one({"_id": "x", "enrichment": {"status": "pending"}})
    assert main(["doctor", "--json"]) == 0  # warnings do not make it unhealthy
    _, checks = _checks(capsys)
    assert checks["webhook_missed"]["status"] == "warn"
    assert "3" in checks["webhook_missed"]["message"]
    assert checks["enrichment_backlog"]["status"] == "warn"
    assert checks["recent_errors"]["status"] == "warn"


def test_nothing_configured_is_info_only_and_probes_nothing(tmp_path, monkeypatch, capsys):
    rt = install(
        monkeypatch,
        FakeRuntime(
            tmp_path,
            POCKETSYNC_API_KEY="",
            POCKETSYNC_MONGO_URI="",
            POCKETSYNC_WEBHOOK_SECRET="",
        ),
    )
    rt.probe_result = AssertionError("must not probe")
    rt.ping_error = AssertionError("must not ping")
    assert main(["doctor", "--json"]) == 0
    report, checks = _checks(capsys)
    assert report["healthy"] is True
    assert checks["runtime_config"]["status"] == "info"
    assert not RUNTIME_IDS & set(checks)


def test_wheel_install_nothing_configured_exits_0(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(doctor_cmd, "find_culture_yaml", lambda: None)
    monkeypatch.setattr(
        _runtime,
        "build",
        lambda: FakeRuntime(
            tmp_path, POCKETSYNC_API_KEY="", POCKETSYNC_MONGO_URI="", POCKETSYNC_WEBHOOK_SECRET=""
        ),
    )
    assert main(["doctor", "--json"]) == 0
    report, checks = _checks(capsys)
    assert report["healthy"] is True
    assert all(c["severity"] == "info" for c in checks.values())


def test_bad_config_reports_fail_not_traceback(monkeypatch, capsys):
    def bad():
        raise CliError(
            2, "invalid TOML in /x.toml: oops", "Fix the TOML syntax in the config file."
        )

    monkeypatch.setattr(_runtime, "build", bad)
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "config" in out and "Traceback" not in out


def test_text_mode_marks(rt, capsys):
    rt.ping_error = RuntimeError("nope")
    main(["doctor"])
    out = capsys.readouterr().out
    assert "[FAIL] mongo_auth" in out and "hint:" in out
    assert "[warn]" in out
