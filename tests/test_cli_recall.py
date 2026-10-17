"""CLI verbs over the recall core: enrich, search, get, transcript, recordings, actions.

All data is synthetic and lives in mongomock (via :class:`tests.cli_fakes.FakeRuntime`).
"""

from __future__ import annotations

import copy
import json

import pytest

from pocketsync.cli import main
from pocketsync.enrich.gateway import EMBED_DIMS
from tests.cli_fakes import FakeRuntime, install

VEC = [0.25] * EMBED_DIMS


def _doc(rid, at, *, labels=("meeting",), status="done", sensitive=None, actions=(), **kw):
    filters = {} if sensitive is None else {"sensitive": sensitive}
    doc = {
        "_id": rid,
        "title": f"Synthetic {rid}",
        "recording_at": at,
        "duration": 60.0,
        "summary": f"Synthetic summary about budgets for {rid}.",
        "segments": [
            {"start": 0.0, "end": 5.0, "text": "hello there", "speaker": "A"},
            {"start": 5.0, "end": 10.0, "text": "we decided on budgets", "speaker": "B"},
            {"start": 10.0, "end": 15.0, "text": "goodbye", "speaker": "A"},
        ],
        "action_items": [dict(a) for a in actions],
        "content_hash": f"hash-{rid}",
        "deleted_in_pocket": False,
        "merged_into": None,
        "enrichment": {
            "status": status,
            "attempts": 0,
            "labels": list(labels),
            "filters": filters,
            "summary_vector": VEC,
            "chunks": [
                {
                    "index": 0,
                    "start": 0.0,
                    "end": 15.0,
                    "text": "we decided on budgets",
                    "vector": VEC,
                },
            ],
        },
    }
    doc.update(kw)
    return doc


@pytest.fixture
def rt(tmp_path, monkeypatch):
    runtime = install(monkeypatch, FakeRuntime(tmp_path))
    col = runtime.db["recordings"]
    col.insert_many(
        [
            _doc("r0001", "2026-10-01T09:00:00Z", actions=[{"text": "send notes", "done": False}]),
            _doc(
                "r0002",
                "2026-10-05T09:00:00Z",
                labels=("personal",),
                status="error",
                sensitive=0.9,
                actions=[{"text": "call bank", "done": True}],
            ),
            _doc("r0003", "2026-10-09T09:00:00Z", sensitive=0.1),
            _doc("r0004", "2026-10-10T09:00:00Z", deleted_in_pocket=True),
        ]
    )
    return runtime


def _run_json(capsys, argv):
    rc = main([*argv, "--json"])
    out = capsys.readouterr()
    assert rc == 0, out.err
    return json.loads(out.out)


def _snapshot(rt):
    return copy.deepcopy(sorted(rt.db["recordings"].find(), key=lambda d: d["_id"]))


# -- enrich ---------------------------------------------------------------------------------


def test_enrich_dry_run_lists_ids_and_writes_nothing(rt, capsys):
    before = _snapshot(rt)
    out = _run_json(capsys, ["enrich", "--all"])
    assert out["dry_run"] is True
    assert out["ids"] == ["r0001", "r0002", "r0003"]
    assert out["count"] == 3
    assert out["mode"] == "full"
    assert out["estimated_gateway_calls"] == 6
    assert out["updated"] == 0
    assert _snapshot(rt) == before


def test_enrich_dry_run_text_shows_ids_count_and_estimate(rt, capsys):
    assert main(["enrich", "--status", "error"]) == 0
    text = capsys.readouterr().out
    assert "r0002" in text
    assert "1 recording" in text
    assert "2 gateway call" in text
    assert "--apply" in text


def test_enrich_apply_requeues_only_selected(rt, capsys):
    out = _run_json(
        capsys, ["enrich", "--id", "r0001", "--id", "r0003", "--only", "classify", "--apply"]
    )
    assert out["dry_run"] is False
    assert out["updated"] == 2
    col = rt.db["recordings"]
    assert col.find_one({"_id": "r0001"})["enrichment"]["status"] == "pending"
    assert col.find_one({"_id": "r0001"})["enrichment"]["mode"] == "classify"
    assert col.find_one({"_id": "r0002"})["enrichment"]["status"] == "error"


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["--since", "2026-10-05"], ["r0002", "r0003"]),
        (["--until", "2026-10-05"], ["r0001", "r0002"]),
        (["--id", "r0003"], ["r0003"]),
        (["--label", "personal"], ["r0002"]),
        (["--status", "error"], ["r0002"]),
        (["--all", "--only", "embed"], ["r0001", "r0002", "r0003"]),
    ],
)
def test_enrich_selectors(rt, capsys, argv, expected):
    assert _run_json(capsys, ["enrich", *argv])["ids"] == expected


def test_enrich_without_selector_is_a_user_error(rt, capsys):
    assert main(["enrich"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


def test_enrich_bad_only_choice_is_a_user_error(rt, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["enrich", "--all", "--only", "bogus"])
    assert exc.value.code == 1
    assert "hint:" in capsys.readouterr().err


def test_enrich_requires_mongo(tmp_path, monkeypatch, capsys):
    install(monkeypatch, FakeRuntime(tmp_path, POCKETSYNC_MONGO_URI=""))
    assert main(["enrich", "--all"]) == 2
    assert "hint:" in capsys.readouterr().err


# -- search ---------------------------------------------------------------------------------


def test_search_vector_mode_uses_query_prompt(rt, capsys):
    out = _run_json(capsys, ["search", "budgets"])
    assert out["mode"] == "vector"
    assert {r["id"] for r in out["results"]} == {"r0001", "r0002", "r0003"}
    assert rt.fake_gateway.prompt_names[-1] == "query"
    assert "vector" not in json.dumps(out["results"][0]["chunks"])


def test_search_filters_and_sensitive_opt_in(rt, capsys):
    out = _run_json(capsys, ["search", "budgets", "--label", "meeting", "--exclude-sensitive"])
    assert {r["id"] for r in out["results"]} == {"r0001", "r0003"}
    out = _run_json(capsys, ["search", "budgets", "--limit", "1"])
    assert out["count"] == 1


def test_search_falls_back_to_keyword_when_embed_fails(rt, capsys):
    rt.fake_gateway.embed_error = True
    out = _run_json(capsys, ["search", "budgets"])
    assert out["mode"] == "keyword"
    assert "reason" in out
    assert {r["id"] for r in out["results"]} == {"r0001", "r0002", "r0003"}


def test_search_text_is_concise_and_has_no_vectors(rt, capsys):
    assert main(["search", "budgets"]) == 0
    text = capsys.readouterr().out
    assert "r0001" in text
    assert "0.25, 0.25" not in text
    assert len(text) < 2000


def test_search_empty_query_is_user_error(rt, capsys):
    assert main(["search", "  "]) == 1


# -- get / transcript -----------------------------------------------------------------------


def test_get_json_has_no_vectors(rt, capsys):
    out = _run_json(capsys, ["get", "r0001"])
    assert out["id"] == "r0001"
    assert out["action_items"] == [{"text": "send notes", "done": False, "owner": None}]
    assert "vector" not in json.dumps(out)


def test_get_text(rt, capsys):
    assert main(["get", "r0001"]) == 0
    text = capsys.readouterr().out
    assert "Synthetic r0001" in text
    assert "send notes" in text


def test_get_unknown_id_is_user_error(rt, capsys):
    assert main(["get", "nope"]) == 1
    assert "hint:" in capsys.readouterr().err


def test_transcript_window(rt, capsys):
    out = _run_json(capsys, ["transcript", "r0001", "--start", "6", "--end", "9"])
    assert [s["text"] for s in out["segments"]] == ["we decided on budgets"]
    full = _run_json(capsys, ["transcript", "r0001"])
    assert len(full["segments"]) == 3


def test_transcript_text_lines(rt, capsys):
    assert main(["transcript", "r0001"]) == 0
    text = capsys.readouterr().out
    assert "we decided on budgets" in text
    assert "B" in text


# -- recordings / actions -------------------------------------------------------------------


def test_recordings_newest_first_and_filters(rt, capsys):
    out = _run_json(capsys, ["recordings"])
    assert [r["id"] for r in out["recordings"]] == ["r0003", "r0002", "r0001"]
    out = _run_json(capsys, ["recordings", "--exclude-sensitive", "--limit", "1"])
    assert [r["id"] for r in out["recordings"]] == ["r0003"]
    out = _run_json(capsys, ["recordings", "--label", "personal"])
    assert [r["id"] for r in out["recordings"]] == ["r0002"]


def test_recordings_text(rt, capsys):
    assert main(["recordings"]) == 0
    text = capsys.readouterr().out
    assert "r0003" in text
    assert "Synthetic r0003" in text


def test_actions_open_only_by_default(rt, capsys):
    out = _run_json(capsys, ["actions"])
    assert [a["text"] for a in out["action_items"]] == ["send notes"]
    out = _run_json(capsys, ["actions", "--no-open-only"])
    assert [a["text"] for a in out["action_items"]] == ["call bank", "send notes"]
    out = _run_json(capsys, ["actions", "--no-open-only", "--exclude-sensitive"])
    assert [a["text"] for a in out["action_items"]] == ["send notes"]


def test_actions_text(rt, capsys):
    assert main(["actions"]) == 0
    assert "send notes" in capsys.readouterr().out


@pytest.mark.parametrize(
    "verb", ["search budgets", "get r0001", "transcript r0001", "recordings", "actions"]
)
def test_read_verbs_write_nothing(rt, capsys, verb):
    before = _snapshot(rt)
    assert main([*verb.split(), "--json"]) == 0
    assert _snapshot(rt) == before


@pytest.mark.parametrize(
    "verb", ["search x", "get r0001", "transcript r0001", "recordings", "actions"]
)
def test_read_verbs_require_mongo(tmp_path, monkeypatch, capsys, verb):
    install(monkeypatch, FakeRuntime(tmp_path, POCKETSYNC_MONGO_URI=""))
    assert main(verb.split()) == 2


# -- runtime chat accessor ------------------------------------------------------------------


def test_runtime_chat_uses_chat_model_setting(tmp_path):
    from pocketsync.cli._runtime import Runtime
    from pocketsync.config import load

    rt = Runtime(load(env={"POCKETSYNC_CHAT_MODEL": "synthetic/chat-model"}))
    client = rt.chat()
    assert client.chat_model == "synthetic/chat-model"
    assert rt.chat() is client


def test_fake_runtime_overrides_chat(rt):
    assert rt.chat() is rt.fake_chat
    assert rt.chat().chat([{"role": "user", "content": "hi"}])
