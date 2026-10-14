"""vindex tests against a recording stub collection (no Mongo, no network)."""

import subprocess
import sys

import pytest

from pocketsync import vindex
from pocketsync.cli._errors import CliError


class _Cursor(list):
    def limit(self, n):
        return _Cursor(self[:n])


class _Meta:
    def __init__(self):
        self.doc = None

    def find_one(self, q):
        return self.doc

    def replace_one(self, q, doc, upsert=False):
        self.doc = doc


class _DB(dict):
    def __missing__(self, k):
        self[k] = _Meta()
        return self[k]


class Stub:
    def __init__(self, docs=()):
        self.docs = list(docs)
        self.indexes = []
        self.created = []
        self.finds = []
        self.database = _DB()

    def list_search_indexes(self):
        return list(self.indexes)

    def create_search_index(self, model):
        self.created.append(model)
        self.indexes.append({"name": model["name"], "latestDefinition": model["definition"]})

    def find(self, q, proj=None):
        self.finds.append(q)
        bad = [
            {"_id": d["_id"]}
            for d in self.docs
            if d["enrichment"]["model"] != "m" or d["enrichment"]["dims"] != 4
        ]
        return _Cursor(bad)


def _doc(i, model="m", dims=4):
    return {"_id": i, "enrichment": {"model": model, "dims": dims}}


def test_creates_two_vector_indexes():
    c = Stub()
    assert vindex.ensure_index(c, "m", 4) == [vindex.SUMMARY_INDEX, vindex.CHUNK_INDEX]
    s, ch = c.created
    assert s["type"] == ch["type"] == "vectorSearch"
    vec = s["definition"]["fields"][0]
    assert vec == {
        "type": "vector",
        "path": vindex.PATH_SUMMARY_VECTOR,
        "numDimensions": 4,
        "similarity": "cosine",
    }
    filt = {f["path"] for f in s["definition"]["fields"] if f["type"] == "filter"}
    assert filt == {"enrichment.labels", "deleted_in_pocket", "recording_at"}
    assert ch["definition"]["nestedRoot"] == "enrichment.chunks"
    assert ch["definition"]["fields"][0]["path"] == "enrichment.chunks.vector"


def test_idempotent():
    c = Stub()
    vindex.ensure_index(c, "m", 4)
    assert vindex.ensure_index(c, "m", 4) == []
    assert len(c.created) == 2


def test_dimension_change_fails_with_remediation():
    c = Stub()
    vindex.ensure_index(c, "m", 4)
    with pytest.raises(CliError) as e:
        vindex.check(c, "m", 8)
    assert "re-embed --apply" in e.value.remediation.replace("reembed", "re-embed")
    assert "reembed --apply" in e.value.remediation


def test_model_change_fails():
    c = Stub()
    vindex.ensure_index(c, "m", 4)
    with pytest.raises(CliError):
        vindex.ensure_index(c, "other", 4)


def test_stored_doc_mismatch_fails_and_plan_lists_ids():
    c = Stub([_doc(1), _doc(2, model="x"), _doc(3, dims=9)])
    with pytest.raises(CliError):
        vindex.check(c, "m", 4)
    assert vindex.reembed_plan(c, "m", 4) == [2, 3]
    assert not c.created


def test_clean_store_passes():
    vindex.check(Stub([_doc(1)]), "m", 4)


def test_import_without_pymongo():
    code = "import sys; sys.modules['pymongo']=None; import pocketsync.vindex"
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
