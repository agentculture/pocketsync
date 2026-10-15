"""`pocketsync sync`: dry-run by default with 0 writes; --apply lands, enriches, drains."""

from __future__ import annotations

import json

import pytest

from pocketsync.cli import main
from tests.cli_fakes import FakeRuntime, install, rec, ts


@pytest.fixture
def rt(tmp_path, monkeypatch):
    return install(monkeypatch, FakeRuntime(tmp_path, [rec(1), rec(2)]))


def test_dry_run_is_default_and_performs_zero_writes(rt, capsys):
    assert main(["sync", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True
    assert out["result"]["landed"] == 2
    assert out["planned"]  # describes what --apply would do
    assert rt.total_writes() == 0
    assert rt.files_on_disk() == []
    assert rt.docs_in_db() == 0
    assert "get_audio_url" not in rt.pocket.calls
    assert out["enrichment"] is None
    assert out["outbox"] is None


def test_dry_run_backfill_zero_writes_even_on_errors(rt, capsys):
    rt.pocket.set_rate_limit(100)  # every call fails -> reconciler records an error
    rc = main(["sync", "--since", "2020-01-01", "--json"])
    assert rc == 2
    cap = capsys.readouterr()
    out = json.loads(cap.out)
    assert out["mode"] == "backfill"
    assert out["result"]["ok"] is False
    assert rt.total_writes() == 0
    assert rt.docs_in_db() == 0
    assert json.loads(cap.err)["remediation"]


def test_dry_run_text_mode_says_dry_run(rt, capsys):
    assert main(["sync"]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "--apply" in out


def test_apply_lands_enriches_and_publishes(rt, capsys):
    assert main(["sync", "--apply", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is False
    assert out["mode"] == "reconcile"
    assert out["result"]["landed"] == 2
    assert out["enrichment"]["done"] == 2
    assert out["outbox"]["published"] == 2
    assert out["outbox"]["pending"] == 0
    assert len(rt.events_client.published) == 2
    assert rt.db["recordings"].count_documents({"enrichment.status": "done"}) == 2
    assert any(p.endswith("audio.mp3") for p in rt.files_on_disk())
    assert rt.counting_state.get_watermark() is not None


def test_apply_is_idempotent(rt, capsys):
    assert main(["sync", "--apply", "--json"]) == 0
    capsys.readouterr()
    assert main(["sync", "--apply", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["result"]["landed"] == 0
    assert out["result"]["skipped"] == 2
    assert out["outbox"]["published"] == 0


def test_no_audio_skips_audio(rt, capsys):
    assert main(["sync", "--apply", "--no-audio", "--no-enrich", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["enrichment"] is None
    assert out["outbox"] is None
    assert not any("audio" in p for p in rt.files_on_disk())
    assert "get_audio_url" not in rt.pocket.calls
    assert rt.db["recordings"].count_documents({"enrichment.status": "pending"}) == 2


def test_since_runs_backfill_without_watermark(tmp_path, monkeypatch, capsys):
    old = rec(9, days_ago=30)
    rt = install(monkeypatch, FakeRuntime(tmp_path, [old, rec(1)]))
    assert main(["sync", "--since", ts(40)[:10], "--apply", "--no-enrich", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "backfill"
    assert out["result"]["landed"] == 2
    assert rt.counting_state.get_watermark() is None


def test_bad_since_is_user_error(rt, capsys):
    assert main(["sync", "--since", "yesterday-ish"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert rt.total_writes() == 0


def test_requires_mongo(tmp_path, monkeypatch, capsys):
    install(monkeypatch, FakeRuntime(tmp_path, [rec(1)], POCKETSYNC_MONGO_URI=""))
    assert main(["sync", "--json"]) == 2
    assert "POCKETSYNC_MONGO_URI" in json.loads(capsys.readouterr().err)["message"]
