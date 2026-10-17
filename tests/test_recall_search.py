"""Unit tests for pocketsync.recall.search (synthetic data; $vectorSearch is faked)."""

from __future__ import annotations

import json

import mongomock
import pytest

from pocketsync import vindex
from pocketsync.cli._errors import CliError
from pocketsync.enrich.gateway import EnrichmentUnavailable
from pocketsync.recall.search import search
from pocketsync.sinks.mongo import MongoSink

QVEC = [1.0, 0.0, 0.0]


class FakeGateway:
    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.calls: list[tuple[list[str], str]] = []

    def embed(self, texts, prompt_name="Document"):
        self.calls.append((list(texts), prompt_name))
        if self.fail is not None:
            raise self.fail
        return [list(QVEC) for _ in texts]


class FakeSearch:
    """Records pipelines; returns canned docs per index (ignores filters on purpose)."""

    def __init__(self, chunk_docs=(), summary_docs=()) -> None:
        self.pipelines: list[list[dict]] = []
        self.docs = {vindex.CHUNK_INDEX: list(chunk_docs), vindex.SUMMARY_INDEX: list(summary_docs)}

    def __call__(self, collection, pipeline):
        self.pipelines.append(pipeline)
        return [dict(d) for d in self.docs[pipeline[0]["$vectorSearch"]["index"]]]

    def stage(self, index: str) -> dict:
        for p in self.pipelines:
            if p[0]["$vectorSearch"]["index"] == index:
                return p[0]["$vectorSearch"]
        raise AssertionError(f"no pipeline for {index}")

    def post_match(self, index: str) -> dict:
        for p in self.pipelines:
            if p[0]["$vectorSearch"]["index"] == index:
                return next(s["$match"] for s in p if "$match" in s)
        raise AssertionError(f"no pipeline for {index}")


def _chunk(index, vec, text="chunk", start=0.0, end=5.0):
    return {"index": index, "start": start, "end": end, "text": text, "vector": vec}


def _doc(rid, score=0.9, sensitive=None, **kw):
    filters = {} if sensitive is None else {"sensitive": sensitive}
    doc = {
        "_id": rid,
        "title": f"Synthetic {rid}",
        "recording_at": "2026-10-10T10:00:00Z",
        "deleted_in_pocket": False,
        "merged_into": None,
        "score": score,
        "enrichment": {
            "labels": ["meeting"],
            "filters": filters,
            "chunks": [
                _chunk(0, [0.0, 1.0, 0.0], "off topic", 0.0, 10.0),
                _chunk(1, [0.9, 0.1, 0.0], "on topic", 10.0, 20.0),
            ],
        },
    }
    doc.update(kw)
    return doc


def _assert_no_vectors(obj) -> None:
    if isinstance(obj, dict):
        assert not any("vector" in k for k in obj), obj
        for v in obj.values():
            _assert_no_vectors(v)
    elif isinstance(obj, list):
        for v in obj:
            _assert_no_vectors(v)


@pytest.fixture
def col():
    return mongomock.MongoClient()["pocketsync"][vindex.COLLECTION]


def test_query_is_embedded_with_prompt_name_query(col) -> None:
    gw = FakeGateway()
    search(col, gw, "budget", search_fn=FakeSearch())
    assert gw.calls == [(["budget"], "query")]


def test_label_filter_goes_in_parent_filter_for_chunks_and_filter_for_summary(col) -> None:
    fs = FakeSearch()
    search(col, FakeGateway(), "budget", labels=["meeting"], search_fn=fs)
    chunk = fs.stage(vindex.CHUNK_INDEX)
    summ = fs.stage(vindex.SUMMARY_INDEX)
    assert chunk["path"] == vindex.PATH_CHUNK_VECTOR
    assert summ["path"] == vindex.PATH_SUMMARY_VECTOR
    assert "filter" not in chunk
    assert "parentFilter" not in summ
    assert chunk["parentFilter"] == summ["filter"]
    flt = json.dumps(chunk["parentFilter"])
    assert vindex.PATH_LABELS in flt
    assert "meeting" in flt
    assert vindex.PATH_DELETED in flt
    assert chunk["queryVector"] == QVEC == summ["queryVector"]


def test_every_pipeline_excludes_merged_after_search_stage(col) -> None:
    fs = FakeSearch()
    search(col, FakeGateway(), "budget", search_fn=fs)
    for idx in (vindex.CHUNK_INDEX, vindex.SUMMARY_INDEX):
        assert fs.post_match(idx)["merged_into"] is None


def test_results_merge_by_recording_and_never_include_vectors(col) -> None:
    fs = FakeSearch(
        chunk_docs=[_doc("r1", 0.8), _doc("r2", 0.95)],
        summary_docs=[_doc("r1", 0.97), _doc("r3", 0.5)],
    )
    out = search(col, FakeGateway(), "budget", search_fn=fs)
    assert out["mode"] == "vector"
    ids = [r["id"] for r in out["results"]]
    assert ids == ["r1", "r2", "r3"]  # r1 takes its best score (0.97) across both indexes
    r1 = out["results"][0]
    assert r1["score"] == pytest.approx(0.97)
    assert r1["title"] == "Synthetic r1"
    assert r1["recording_at"] == "2026-10-10T10:00:00Z"
    best = r1["chunks"][0]
    assert (best["index"], best["start"], best["end"], best["text"]) == (1, 10.0, 20.0, "on topic")
    assert r1["chunks"][0]["score"] > r1["chunks"][1]["score"]
    _assert_no_vectors(out)


def test_limit_caps_results(col) -> None:
    fs = FakeSearch(chunk_docs=[_doc(f"r{i}", 0.5 + i / 100) for i in range(8)])
    out = search(col, FakeGateway(), "budget", limit=3, search_fn=fs)
    assert [r["id"] for r in out["results"]] == ["r7", "r6", "r5"]
    assert out["count"] == 3


def test_deleted_and_merged_never_appear_even_if_the_index_returns_them(col) -> None:
    fs = FakeSearch(
        chunk_docs=[_doc("ok"), _doc("gone", deleted_in_pocket=True), _doc("m", merged_into="ok")]
    )
    out = search(col, FakeGateway(), "budget", search_fn=fs)
    assert [r["id"] for r in out["results"]] == ["ok"]


@pytest.mark.parametrize(
    "exclude, expected",
    [(False, {"hi", "lo", "absent", "edge"}), (True, {"lo", "absent"})],
)
def test_exclude_sensitive_is_opt_in_and_keeps_absent_scores(col, exclude, expected) -> None:
    docs = [
        _doc("hi", sensitive=0.9),
        _doc("lo", sensitive=0.1),
        _doc("absent"),
        _doc("edge", sensitive=0.5),
    ]
    fs = FakeSearch(chunk_docs=docs)
    out = search(col, FakeGateway(), "x", exclude_sensitive=exclude, search_fn=fs)
    assert {r["id"] for r in out["results"]} == expected
    post = json.dumps(fs.post_match(vindex.CHUNK_INDEX))
    assert (vindex.PATH_SENSITIVE in post) is exclude


def test_since_until_and_has_action_items_filters(col) -> None:
    docs = [
        _doc("early", recording_at="2026-09-01T00:00:00Z"),
        _doc("in", recording_at="2026-10-14T23:30:00Z"),
        _doc("late", recording_at="2026-10-20T00:00:00Z"),
    ]
    docs[1]["enrichment"]["filters"]["has_action_items"] = 0.8
    docs[0]["enrichment"]["filters"]["has_action_items"] = 0.8
    fs = FakeSearch(chunk_docs=docs)
    out = search(col, FakeGateway(), "x", since="2026-10-01", until="2026-10-14", search_fn=fs)
    assert [r["id"] for r in out["results"]] == ["in"]  # a bare-date until includes that day
    fs = FakeSearch(chunk_docs=docs)
    out = search(col, FakeGateway(), "x", has_action_items=True, search_fn=fs)
    assert {r["id"] for r in out["results"]} == {"early", "in"}
    assert vindex.PATH_HAS_ACTION_ITEMS in json.dumps(fs.stage(vindex.CHUNK_INDEX))


def test_invalid_arguments_raise_cli_error(col) -> None:
    gw, fs = FakeGateway(), FakeSearch()
    with pytest.raises(CliError):
        search(col, gw, "x", limit=0, search_fn=fs)
    with pytest.raises(CliError):
        search(col, gw, "x", since="yesterday", search_fn=fs)
    with pytest.raises(CliError):
        search(col, gw, "   ", search_fn=fs)


def test_accepts_a_mongo_sink_as_store() -> None:
    sink = MongoSink(mongomock.MongoClient()["pocketsync"])
    seen = []
    search(sink, FakeGateway(), "x", search_fn=lambda c, p: seen.append(c) or [])
    assert seen
    assert all(c is sink.collection for c in seen)


def _seed_keyword(col) -> None:
    col.insert_many(
        [
            {
                "_id": "k1",
                "title": "Quarterly BUDGET review",
                "recording_at": "2026-10-10T10:00:00Z",
                "summary": "We reviewed numbers.",
                "deleted_in_pocket": False,
                "merged_into": None,
                "enrichment": {
                    "labels": ["meeting"],
                    "filters": {"sensitive": 0.9},
                    "chunks": [
                        _chunk(0, [1.0], "nothing here", 0.0, 4.0),
                        _chunk(1, [1.0], "the budget is tight, budget!", 4.0, 9.0),
                    ],
                },
            },
            {
                "_id": "k2",
                "title": "Walk",
                "recording_at": "2026-10-11T10:00:00Z",
                "summary": "Talked about the Budget.",
                "deleted_in_pocket": False,
                "merged_into": None,
                "enrichment": {"labels": ["personal"], "filters": {}},
            },
            {
                "_id": "k3",
                "title": "budget deleted",
                "recording_at": "2026-10-12T10:00:00Z",
                "deleted_in_pocket": True,
                "merged_into": None,
            },
            {
                "_id": "k4",
                "title": "budget merged",
                "recording_at": "2026-10-12T10:00:00Z",
                "deleted_in_pocket": False,
                "merged_into": "k1",
            },
            {"_id": "k5", "title": "Unrelated", "deleted_in_pocket": False, "merged_into": None},
        ]
    )


@pytest.mark.parametrize(
    "fail", [EnrichmentUnavailable("embed lane down"), CliError(2, "bad dims", "check model")]
)
def test_embed_failure_falls_back_to_keyword_mode(col, fail) -> None:
    _seed_keyword(col)
    fs = FakeSearch()
    out = search(col, FakeGateway(fail=fail), "budget", search_fn=fs)
    assert fs.pipelines == []  # no vector search attempted
    assert out["mode"] == "keyword"
    assert out["reason"]
    ids = [r["id"] for r in out["results"]]
    assert ids == ["k1", "k2"]  # k1 has more matches; deleted/merged/unmatched excluded
    k1 = out["results"][0]
    assert [(c["index"], c["start"], c["end"]) for c in k1["chunks"]] == [(1, 4.0, 9.0)]
    _assert_no_vectors(out)


def test_keyword_mode_applies_recording_level_filters(col) -> None:
    _seed_keyword(col)
    gw = FakeGateway(fail=EnrichmentUnavailable("down"))
    out = search(col, gw, "budget", labels=["personal"], search_fn=FakeSearch())
    assert [r["id"] for r in out["results"]] == ["k2"]
    out = search(col, gw, "budget", exclude_sensitive=True, search_fn=FakeSearch())
    assert [r["id"] for r in out["results"]] == ["k2"]
    out = search(col, gw, "budget", since="2026-10-11", search_fn=FakeSearch())
    assert [r["id"] for r in out["results"]] == ["k2"]


def test_keyword_mode_escapes_regex_metacharacters(col) -> None:
    _seed_keyword(col)
    gw = FakeGateway(fail=EnrichmentUnavailable("down"))
    out = search(col, gw, "budget!", search_fn=FakeSearch())
    assert [r["id"] for r in out["results"]] == ["k1"]
    out = search(col, gw, ".*", search_fn=FakeSearch())
    assert out["results"] == []


def test_vector_search_failure_falls_back_to_keyword_mode(col) -> None:
    """An index that is mid-rebuild or lacks a filter field must not turn into a 500."""
    _seed_keyword(col)

    def broken(collection, pipeline):
        raise RuntimeError("synthetic: path is not indexed as a filter")

    gw = FakeGateway()
    out = search(col, gw, "budget", has_action_items=None, search_fn=broken)
    assert out["mode"] == "keyword"
    assert out["reason"] == "vector search failed: RuntimeError"
    assert [r["id"] for r in out["results"]] == ["k1", "k2"]
    assert gw.calls  # the query was embedded first; only the search stage failed
