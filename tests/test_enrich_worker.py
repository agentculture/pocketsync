"""EnrichmentWorker: pending queue, CAS, backoff, concurrency cap, single event (synthetic)."""

from __future__ import annotations

import json
import threading
import time

import mongomock
import pytest

from pocketsync import vindex
from pocketsync.cli._errors import CliError
from pocketsync.enrich.gateway import (
    EMBED_DIMS,
    EMBED_MODEL,
    Classification,
    EnrichmentUnavailable,
)
from pocketsync.enrich.worker import EnrichmentWorker, mark_for_reclassification
from pocketsync.events import Outbox
from pocketsync.model import Recording, TranscriptSegment
from pocketsync.sinks.base import FileRef, FileSet
from pocketsync.sinks.mongo import MongoSink

TAX = "tax-v1"


class FakeGateway:
    def __init__(self, fail_first=0, retry_after=None, delay=0.0, on_embed=None, tax=TAX):
        self.fail_first = fail_first
        self.retry_after = retry_after
        self.delay = delay
        self.on_embed = on_embed
        self.tax = tax
        self.calls = 0
        self.embed_inputs = []
        self.classify_inputs = []
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def _enter(self):
        with self._lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            n = self.calls
        return n

    def _exit(self):
        with self._lock:
            self.active -= 1

    def embed(self, texts):
        n = self._enter()
        try:
            self.embed_inputs.append(list(texts))
            if n <= self.fail_first:
                raise EnrichmentUnavailable("503", retry_after=self.retry_after)
            if self.on_embed:
                self.on_embed()
            time.sleep(self.delay)
            return [[0.5] * EMBED_DIMS for _ in texts]
        finally:
            self._exit()

    def classify(self, text):
        self.classify_inputs.append(text)
        return Classification(["meeting"], 0.9, {"meeting": 0.9}, {"urgent": 0.1}, self.tax)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def store():
    return MongoSink(mongomock.MongoClient()["pocketsync"])


@pytest.fixture
def outbox(tmp_path):
    return Outbox(tmp_path / "out")


def rec(rid="r1", summary="a summary", text="hello there"):
    segs = [TranscriptSegment(start=0, end=1, text=text, speaker="A")] if text else []
    return Recording(
        id=rid,
        title="T",
        recording_at="2026-05-01T10:00:00Z",
        updated_at="2026-05-01T11:00:00Z",
        created_at="2026-05-01T10:59:00Z",
        summary=summary,
        segments=segs,
    )


def events(outbox):
    p = outbox.path
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def doc(store, rid="r1"):
    return store.collection.find_one({"_id": rid})


def test_success_writes_enrichment_and_one_event(store, outbox):
    files = FileSet(transcript_md=FileRef("2026/05/r1/transcript.md", 3, "ab"))
    store.put_recording(rec(), files)
    gw = FakeGateway()
    w = EnrichmentWorker(store, gw, outbox)
    stats = w.run_once()
    assert stats["done"] == 1
    e = doc(store)["enrichment"]
    assert e["status"] == "done"
    assert e["model"] == EMBED_MODEL and e["dims"] == EMBED_DIMS
    assert e["labels"] == ["meeting"] and e["taxonomy_version"] == TAX
    assert len(e["summary_vector"]) == EMBED_DIMS
    assert e["chunks"][0]["index"] == 0 and len(e["chunks"][0]["vector"]) == EMBED_DIMS
    assert e["enriched_at"]
    evs = events(outbox)
    assert len(evs) == 1
    assert evs[0]["recording_id"] == "r1"
    assert evs[0]["content_hash"] == doc(store)["content_hash"]
    assert evs[0]["enrichment"]["labels"] == ["meeting"]
    assert evs[0]["sinks"]["files"]["transcript_md"] == "2026/05/r1/transcript.md"
    assert evs[0]["sinks"]["mongo"] == {"collection": vindex.COLLECTION, "doc_id": "r1"}
    # second run: nothing pending, no more events
    assert w.run_once()["done"] == 0
    assert len(events(outbox)) == 1


def test_503_then_200_stays_pending_with_backoff(store, outbox):
    store.put_recording(rec())
    clk = Clock()
    gw = FakeGateway(fail_first=1, retry_after="30")
    w = EnrichmentWorker(store, gw, outbox, clock=clk)
    s1 = w.run_once()
    assert s1["unavailable"] == 1 and s1["done"] == 0
    assert doc(store)["enrichment"]["status"] == "pending"
    assert events(outbox) == []
    # still inside the Retry-After window -> not retried
    clk.t += 10
    assert w.run_once()["deferred"] == 1
    assert gw.calls == 1
    clk.t += 25
    s3 = w.run_once()
    assert s3["done"] == 1
    assert doc(store)["enrichment"]["status"] == "done"
    assert len(events(outbox)) == 1


def test_backoff_is_exponential_without_retry_after(store, outbox):
    store.put_recording(rec())
    clk = Clock()
    gw = FakeGateway(fail_first=2)
    w = EnrichmentWorker(store, gw, outbox, clock=clk, backoff_base=10, backoff_max=100)
    w.run_once()
    clk.t += 9
    assert w.run_once()["deferred"] == 1
    clk.t += 2  # past 10s
    w.run_once()  # second failure -> 20s
    clk.t += 19
    assert w.run_once()["deferred"] == 1
    clk.t += 2
    assert w.run_once()["done"] == 1


def test_content_change_mid_enrichment_discards_and_requeues(store, outbox):
    store.put_recording(rec())

    def change():
        store.put_recording(rec(summary="changed summary"))

    gw = FakeGateway(on_embed=change)
    w = EnrichmentWorker(store, gw, outbox)
    s = w.run_once()
    assert s["stale"] == 1 and s["done"] == 0
    d = doc(store)
    assert d["enrichment"]["status"] == "pending"
    assert "labels" not in d["enrichment"]
    assert events(outbox) == []
    gw.on_embed = None
    assert w.run_once()["done"] == 1
    evs = events(outbox)
    assert len(evs) == 1 and evs[0]["content_hash"] == doc(store)["content_hash"]


def test_concurrency_cap(store, outbox):
    for i in range(8):
        store.put_recording(rec(f"r{i}"))
    gw = FakeGateway(delay=0.05)
    w = EnrichmentWorker(store, gw, outbox, concurrency=2, batch=8)
    assert w.run_once()["done"] == 8
    assert gw.max_active <= 2
    assert gw.max_active == 2  # the cap is actually used
    assert len(events(outbox)) == 8


def test_default_cap_is_two(store, outbox):
    assert EnrichmentWorker(store, FakeGateway(), outbox).concurrency == 2


def test_single_event_per_content_hash(store, outbox):
    store.put_recording(rec())
    w = EnrichmentWorker(store, FakeGateway(), outbox)
    w.run_once()
    mark_for_reclassification(store, "other-tax")
    w.run_once()  # re-enriched, same content hash
    assert len(events(outbox)) == 1


def test_nonretryable_error_sets_error_status(store, outbox):
    store.put_recording(rec())

    class Bad(FakeGateway):
        def embed(self, texts):
            raise CliError(2, "bad vectors", "check model")

    s = EnrichmentWorker(store, Bad(), outbox).run_once()
    assert s["errors"] == 1
    d = doc(store)["enrichment"]
    assert d["status"] == "error" and "bad vectors" in d["error"]
    assert events(outbox) == []


def test_no_text_is_done_without_vectors(store, outbox):
    store.put_recording(rec(summary="", text=""))
    gw = FakeGateway()
    EnrichmentWorker(store, gw, outbox).run_once()
    e = doc(store)["enrichment"]
    assert e["status"] == "done" and e["labels"] == [] and e["chunks"] == []
    assert "summary_vector" not in e
    assert gw.calls == 0 and gw.classify_inputs == []
    assert len(events(outbox)) == 1


def test_missing_summary_still_embeds_and_classifies_transcript(store, outbox):
    store.put_recording(rec(summary=""))
    gw = FakeGateway()
    EnrichmentWorker(store, gw, outbox).run_once()
    e = doc(store)["enrichment"]
    assert "summary_vector" not in e and len(e["chunks"]) == 1
    assert "hello there" in gw.classify_inputs[0]


def test_mark_for_reclassification(store, outbox):
    store.put_recording(rec("a"))
    store.put_recording(rec("b"))
    EnrichmentWorker(store, FakeGateway(), outbox).run_once()
    assert mark_for_reclassification(store, TAX) == 0
    assert mark_for_reclassification(store, "tax-v2") == 2
    assert doc(store, "a")["enrichment"]["status"] == "pending"
    assert len(store.pending_enrichment(10)) == 2


def test_reclassify_stale_requeues_only_docs_classified_under_another_taxonomy(store, outbox):
    store.put_recording(rec("r1"))
    store.put_recording(rec("r2", summary="", text=""))  # no text: done, never classified
    EnrichmentWorker(store, FakeGateway(tax="tax-old"), outbox).run_once()
    w = EnrichmentWorker(store, FakeGateway(tax=TAX), outbox)
    assert w.reclassify_stale(TAX) == 1
    assert doc(store, "r1")["enrichment"]["status"] == "pending"
    assert doc(store, "r2")["enrichment"]["status"] == "done"
    assert w.reclassify_stale(TAX) == 0  # r1 is pending now; r2 is never re-queued
