"""`pocketsync auth` and `pocketsync list` against the fake Pocket source (offline)."""

from __future__ import annotations

import json

import pytest

from pocketsync.cli import main
from pocketsync.sync import land
from tests.cli_fakes import FakeRuntime, install, rec


@pytest.fixture
def rt(tmp_path, monkeypatch):
    return install(monkeypatch, FakeRuntime(tmp_path, [rec(1), rec(2), rec(3)]))


def test_auth_json_one_list_call_limit_1(rt, capsys):
    calls = []
    orig = rt.pocket.list_recordings

    def spy(*a, **kw):
        calls.append(kw)
        return orig(*a, **kw)

    rt.pocket.list_recordings = spy
    assert main(["auth", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["total"] == 3
    assert len(calls) == 1 and calls[0]["limit"] == 1
    assert rt.pocket.calls == ["list_recordings"]


def test_auth_never_prints_key(rt, capsys):
    assert main(["auth"]) == 0
    cap = capsys.readouterr()
    assert "pk_synthetic" not in cap.out + cap.err
    assert "ok" in cap.out


def test_auth_missing_key_is_env_error(tmp_path, monkeypatch, capsys):
    install(monkeypatch, FakeRuntime(tmp_path, [], POCKETSYNC_API_KEY=""))
    assert main(["auth", "--json"]) == 2
    err = json.loads(capsys.readouterr().err)
    assert "POCKETSYNC_API_KEY" in err["message"]
    assert err["remediation"]


def test_auth_pocket_error_text_has_hint(rt, capsys):
    rt.pocket.set_rate_limit(1)
    assert main(["auth"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error:") and "hint:" in err


def test_list_reports_sync_status(rt, capsys):
    land("r0001", rt.pocket, files_sink=rt.files, store_sink=rt.store)
    assert main(["list", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    by_id = {r["id"]: r for r in out["recordings"]}
    assert set(by_id) == {"r0001", "r0002", "r0003"}
    assert by_id["r0001"]["synced"] is True
    assert by_id["r0001"]["up_to_date"] is True
    assert by_id["r0001"]["enrichment"] == "pending"
    assert by_id["r0001"]["content_hash"]
    assert by_id["r0002"]["synced"] is False
    assert by_id["r0002"]["up_to_date"] is False
    assert out["total"] == 3


def test_list_detects_remote_edit(rt, capsys):
    land("r0001", rt.pocket, files_sink=rt.files, store_sink=rt.store)
    rt.pocket.edit("r0001", title="changed")
    assert main(["list", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    r1 = next(r for r in out["recordings"] if r["id"] == "r0001")
    assert r1["synced"] is True and r1["up_to_date"] is False


def test_list_limit_and_since(rt, capsys):
    assert main(["list", "--limit", "2", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["recordings"]) == 2 and out["has_more"] is True
    assert main(["list", "--since", "2999-01-01", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["recordings"] == []


@pytest.mark.parametrize("args", [["--limit", "0"], ["--limit", "101"], ["--since", "nope"]])
def test_list_bad_args_are_user_errors(rt, capsys, args):
    assert main(["list", *args]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error:") and "hint:" in err


def test_list_text_mode(rt, capsys):
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "r0001" in out and "not synced" in out


def test_list_without_mongo_reports_unknown_status(tmp_path, monkeypatch, capsys):
    install(monkeypatch, FakeRuntime(tmp_path, [rec(1)], POCKETSYNC_MONGO_URI=""))
    assert main(["list", "--json"]) == 0
    cap = capsys.readouterr()
    out = json.loads(cap.out)
    assert out["recordings"][0]["synced"] is None
    assert "POCKETSYNC_MONGO_URI" in cap.err
