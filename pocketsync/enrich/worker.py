"""Enrichment worker: drain ``enrichment.status == pending`` docs through the gateway.

Per doc: chunk the transcript -> embed chunks + summary (one call) -> classify -> CAS-write
(``set_enrichment`` with the content hash read at pick-up) -> append exactly one outbox event.

Decisions (documented, see the plan's c26/h23/c48/h31/c44):

* **Modes (c5).** ``enrichment.mode`` (set by :meth:`MongoSink.requeue`) picks the work:
  ``full`` embeds and classifies; ``classify`` re-runs the decider on the *stored* summary and
  chunk text and writes only ``labels``/``filters``/``taxonomy_version`` (vectors are kept, no
  embed call); ``embed`` re-embeds and keeps ``labels``/``filters``/``taxonomy_version`` (no
  classify call). A partial mode whose kept half does not exist for the current content (for
  example ``classify`` on a doc never embedded, since a content write drops derived fields)
  falls back to ``full``. Every successful write resets ``mode`` to ``full`` so a later
  content change re-runs everything.
* **Generations (c33).** The ``enrichment.generation`` read at pick-up is passed back as
  ``expected_generation`` on every CAS write (``set_enrichment``, ``set_enrichment_error``,
  ``set_deferred``). A requeue issued while the worker holds the doc bumps the generation, so
  the finishing write misses (counted as ``stale``) and the doc stays ``pending`` for the next
  cycle.
* **Retry/backoff (c7).** :class:`EnrichmentUnavailable` keeps the doc ``pending``. The delay is
  exponential (``backoff_base * 2**(n-1)``, capped at ``backoff_max``), or the gateway's numeric
  ``Retry-After`` when given. It is **persisted** with ``set_deferred``
  (``enrichment.deferred_reason`` + ``enrichment.next_attempt_at``, epoch seconds on the wall
  clock), so a restarted worker skips the doc until it is due and ``status`` can say why the
  backlog is not draining. A success clears both fields. The worker also keeps the schedule and
  failure count in memory (monotonic clock), keyed to the content hash and generation it
  deferred: that drives the exponential growth within one process, and a requeue or content
  change voids the in-memory entry. Any other :class:`CliError` (for example invalid
  JSON or a wrong vector size from a gateway that is restarting) is recorded with
  ``set_enrichment_error`` (status ``error``). It is **retried automatically** on a persisted,
  wall-clock schedule (``enrichment.next_attempt_at``): ``ERROR_RETRY_BASE * 2**(n-1)``,
  capped at ``ERROR_RETRY_MAX``, until ``MAX_ENRICHMENT_ATTEMPTS``. After that it stays
  ``error`` and ``status``/``doctor`` report it as failed, so no outage strands a recording
  silently.
* **Stale results.** If the CAS misses, content changed mid-flight: the result is dropped, no
  event is appended, and the doc is already ``pending`` again from the content write.
* **One event per content hash.** The outbox dedupes on ``(recording_id, content_hash)``, so a
  taxonomy re-classification of unchanged content re-enriches but emits no second
  ``enriched`` event.
* **Reclassified (c6).** When a run writes labels that differ (as a set) from the labels the
  doc already held for the same ``content_hash``, one ``pocketsync.recording.reclassified``
  event is appended (see :mod:`pocketsync.events`); unchanged labels append none.
* **No text at all** (empty summary and transcript): status ``done`` with ``labels == []``,
  ``chunks == []`` and no ``summary_vector``; the gateway is not called.
* **Missing parts.** No summary -> no ``summary_vector``; no transcript -> ``chunks == []``;
  classification uses whatever text exists (summary first, then transcript, truncated).
* **Taxonomy change (c44).** :func:`mark_for_reclassification` flips ``done`` docs whose
  ``enrichment.taxonomy_version`` differs back to ``pending``.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from pocketsync import vindex
from pocketsync.cli._errors import CliError
from pocketsync.enrich.chunking import chunk_segments
from pocketsync.enrich.gateway import EMBED_DIMS, EMBED_MODEL, EnrichmentUnavailable
from pocketsync.events import Outbox
from pocketsync.sinks.mongo import (
    ENRICHMENT_DONE,
    ENRICHMENT_PENDING,
    MAX_ENRICHMENT_ATTEMPTS,
    MODE_CLASSIFY,
    MODE_EMBED,
    MODE_FULL,
    PATH_ENRICHMENT_ATTEMPTS,
    PATH_ENRICHMENT_STATUS,
    MongoSink,
)
from pocketsync.vindex import PATH_DELETED

#: Automatic retry of a failed (``error``) doc: ``ERROR_RETRY_BASE * 2**(attempts-1)`` seconds,
#: capped at ``ERROR_RETRY_MAX``, until ``MAX_ENRICHMENT_ATTEMPTS`` (then terminal). Wall clock,
#: persisted on the doc, so the schedule survives a restart of ``serve``.
ERROR_RETRY_BASE = 60.0
ERROR_RETRY_MAX = 6 * 3600.0
DEFAULT_CONCURRENCY = 2
DEFAULT_BATCH = 10
CLASSIFY_MAX_CHARS = 6000
PATH_TAXONOMY_VERSION = "enrichment.taxonomy_version"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mark_for_reclassification(store: MongoSink, current_taxonomy_version: str) -> int:
    """Set ``done`` docs classified under a different taxonomy back to ``pending``; return count.

    Docs with no text are ``done`` without ever being classified (no ``taxonomy_version``),
    so they are left alone rather than re-queued on every call.
    """
    res = store.collection.update_many(
        {
            PATH_ENRICHMENT_STATUS: ENRICHMENT_DONE,
            PATH_TAXONOMY_VERSION: {"$exists": True, "$ne": current_taxonomy_version},
        },
        {"$set": {PATH_ENRICHMENT_STATUS: ENRICHMENT_PENDING, PATH_ENRICHMENT_ATTEMPTS: 0}},
    )
    return int(res.modified_count)


def _file_paths(files: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for slot, ref in (files or {}).items():
        if isinstance(ref, dict) and ref.get("rel_path"):
            out[slot] = ref["rel_path"]
    return out


def _generation(d: dict[str, Any]) -> int:
    return int((d.get("enrichment") or {}).get("generation") or 0)


def _has_vectors(stored: dict[str, Any], summary: str, chunks: list[Any]) -> bool:
    """True when the stored enrichment already embeds this content (classify may reuse it)."""
    if "model" not in stored:
        return False
    if summary and "summary_vector" not in stored:
        return False
    return not chunks or bool(stored.get("chunks"))


class EnrichmentWorker:
    def __init__(
        self,
        store: MongoSink,
        gateway: Any,
        outbox: Outbox,
        *,
        concurrency: int = DEFAULT_CONCURRENCY,
        batch: int = DEFAULT_BATCH,
        backoff_base: float = 5.0,
        backoff_max: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self.store = store
        self.gateway = gateway
        self.outbox = outbox
        self.concurrency = concurrency
        self.batch = batch
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self._clock = clock
        self._sleep = sleep
        self._wall = wall_clock
        self._lock = threading.Lock()  # guards _retry and outbox appends
        # id -> (next_attempt monotonic, failures, content_hash, generation)
        self._retry: dict[str, tuple[float, int, str, int]] = {}

    # -- scheduling ----------------------------------------------------------------------

    def _defer(self, d: dict[str, Any], gen: int, exc: EnrichmentUnavailable) -> None:
        rid, chash = d["_id"], d["content_hash"]
        with self._lock:
            prev = self._retry.get(rid)
            same = prev is not None and prev[2:] == (chash, gen)
            failures = (prev[1] if same else 0) + 1
            delay = min(self.backoff_base * 2 ** (failures - 1), self.backoff_max)
            try:
                ra = float(exc.retry_after) if exc.retry_after is not None else None
            except (TypeError, ValueError):
                ra = None
            if ra is not None and ra >= 0:
                delay = ra
            self._retry[rid] = (self._clock() + delay, failures, chash, gen)
        self.store.set_deferred(rid, chash, gen, exc.message, self._wall() + delay)

    def _next_attempt(self, d: dict[str, Any]) -> float | None:
        """When to retry a doc that just failed; None once the attempt cap is reached."""
        attempts = int((d.get("enrichment") or {}).get("attempts") or 0) + 1
        if attempts >= MAX_ENRICHMENT_ATTEMPTS:
            return None
        return self._wall() + min(ERROR_RETRY_BASE * 2 ** (attempts - 1), ERROR_RETRY_MAX)

    def _due(self, d: dict[str, Any]) -> bool:
        rid = d["_id"]
        with self._lock:
            entry = self._retry.get(rid)
            if entry is None:
                return True
            if entry[2:] != (d.get("content_hash"), _generation(d)):
                del self._retry[rid]  # requeued or content changed since the deferral
                return True
        return self._clock() >= entry[0]

    def _held_pending(self, skip: set[str]) -> list[dict[str, Any]]:
        """Pending docs this worker deferred itself (its in-memory schedule decides when)."""
        with self._lock:
            ids = [rid for rid in self._retry if rid not in skip]
        if not ids:
            return []
        out = list(
            self.store.collection.find(
                {
                    "_id": {"$in": ids},
                    PATH_ENRICHMENT_STATUS: ENRICHMENT_PENDING,
                    PATH_DELETED: {"$ne": True},
                }
            )
        )
        for d in out:
            enr = d.get("enrichment")
            if not isinstance(enr, dict):
                enr = d["enrichment"] = {}
            enr["generation"] = _generation(d)
        return out

    # -- one doc -------------------------------------------------------------------------

    def reclassify_stale(self, current_taxonomy_version: str | None = None) -> int:
        """Re-queue docs classified under another taxonomy (c44); serve calls this at start."""
        version = current_taxonomy_version or self.gateway.taxonomy.version
        return mark_for_reclassification(self.store, version)

    def _embed_part(self, summary: str, chunks: list[Any]) -> dict[str, Any]:
        vectors = self.gateway.embed(([summary] if summary else []) + [c.text for c in chunks])
        part: dict[str, Any] = {"model": EMBED_MODEL, "dims": EMBED_DIMS}
        if summary:
            part["summary_vector"] = vectors[0]
            vectors = vectors[1:]
        part["chunks"] = [
            {"index": c.index, "start": c.start, "end": c.end, "text": c.text, "vector": v}
            for c, v in zip(chunks, vectors)
        ]
        return part

    def _classify_part(self, summary: str, chunk_texts: list[str]) -> dict[str, Any]:
        body = "\n\n".join(([summary] if summary else []) + chunk_texts)
        cls = self.gateway.classify(body[:CLASSIFY_MAX_CHARS])
        return {
            "labels": list(cls.labels),
            "filters": dict(cls.filters),
            "taxonomy_version": cls.taxonomy_version,
        }

    def _build_payload(self, d: dict[str, Any], mode: str = MODE_FULL) -> dict[str, Any]:
        stored = d.get("enrichment") or {}
        summary = (d.get("summary") or "").strip()
        chunks = chunk_segments(d.get("segments") or [])
        payload: dict[str, Any] = {
            "status": ENRICHMENT_DONE,
            "mode": MODE_FULL,
            "enriched_at": _now_iso(),
            "error": None,
        }
        if not summary and not chunks:
            payload.update(model=EMBED_MODEL, dims=EMBED_DIMS, labels=[], filters={}, chunks=[])
            return payload
        if mode == MODE_CLASSIFY and _has_vectors(stored, summary, chunks):
            texts = [c.get("text") or "" for c in stored.get("chunks") or []]
            payload.update(self._classify_part(summary, texts))
            return payload
        payload.update(self._embed_part(summary, chunks))
        if mode == MODE_EMBED and "taxonomy_version" in stored:
            return payload  # labels/filters/taxonomy_version kept as stored
        payload.update(self._classify_part(summary, [c.text for c in chunks]))
        return payload

    def _process(self, d: dict[str, Any]) -> str:
        rid, chash = d["_id"], d["content_hash"]
        stored = d.get("enrichment") or {}
        gen = _generation(d)
        try:
            payload = self._build_payload(d, stored.get("mode") or MODE_FULL)
        except EnrichmentUnavailable as exc:
            self._defer(d, gen, exc)
            return "unavailable"
        except CliError as exc:
            ok = self.store.set_enrichment_error(
                rid, chash, exc.message, self._next_attempt(d), expected_generation=gen
            )
            return "errors" if ok else "stale"
        if not self.store.set_enrichment(rid, chash, payload, expected_generation=gen):
            return "stale"
        prev_labels = stored.get("labels")
        labels = payload.get("labels", prev_labels or [])
        taxonomy = payload.get("taxonomy_version", stored.get("taxonomy_version"))
        sinks = {
            "mongo": {"collection": vindex.COLLECTION, "doc_id": rid},
            "files": _file_paths(d.get("files")),
        }
        with self._lock:
            self._retry.pop(rid, None)
            enriched = self.outbox.append(
                rid,
                chash,
                labels=labels,
                taxonomy_version=taxonomy,
                model=EMBED_MODEL,
                **sinks,
            )
            changed = prev_labels is not None and set(prev_labels) != set(labels)
            if enriched is None and changed:
                self.outbox.append_reclassified(
                    rid,
                    chash,
                    labels=labels,
                    previous_labels=prev_labels,
                    generation=gen,
                    taxonomy_version=taxonomy,
                    model=EMBED_MODEL,
                    **sinks,
                )
        return "done"

    # -- driving -------------------------------------------------------------------------

    def run_once(self) -> dict[str, int]:
        """Process up to ``batch`` due pending docs, at most ``concurrency`` at a time."""
        stats = dict.fromkeys(("done", "stale", "unavailable", "errors", "deferred"), 0)
        # Over-fetch so backed-off docs at the head of the queue don't starve the rest.
        picked = self.store.pending_enrichment(self.batch + len(self._retry), now=self._wall())
        # Docs this worker deferred are re-read regardless of their persisted next_attempt_at;
        # its own (monotonic) schedule decides. A restarted worker relies on the persisted one.
        picked += self._held_pending({d["_id"] for d in picked})
        todo = []
        for d in picked:
            if not self._due(d):
                stats["deferred"] += 1
            elif len(todo) < self.batch:
                todo.append(d)
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            for outcome in pool.map(self._process, todo):
                stats[outcome] += 1
        return stats

    def run(self, *, interval: float = 5.0, max_cycles: int | None = None) -> None:
        """Poll loop: ``run_once`` then sleep ``interval``; stop after ``max_cycles`` if set."""
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            self.run_once()
            cycles += 1
            if max_cycles is None or cycles < max_cycles:
                self._sleep(interval)
