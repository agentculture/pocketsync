"""Enrichment worker: drain ``enrichment.status == pending`` docs through the gateway.

Per doc: chunk the transcript -> embed chunks + summary (one call) -> classify -> CAS-write
(``set_enrichment`` with the content hash read at pick-up) -> append exactly one outbox event.

Decisions (documented, see the plan's c26/h23/c48/h31/c44):

* **Retry/backoff.** :class:`EnrichmentUnavailable` keeps the doc ``pending``. The next-attempt
  time and failure count live **in memory** (per worker instance): exponential
  (``backoff_base * 2**(n-1)``, capped at ``backoff_max``), or the gateway's numeric
  ``Retry-After`` when given. A restarted worker simply retries immediately; that is safe
  because the doc never left ``pending``. Any other :class:`CliError` (for example invalid
  JSON or a wrong vector size from a gateway that is restarting) is recorded with
  ``set_enrichment_error`` (status ``error``). It is **retried automatically** on a persisted,
  wall-clock schedule (``enrichment.next_attempt_at``): ``ERROR_RETRY_BASE * 2**(n-1)``,
  capped at ``ERROR_RETRY_MAX``, until ``MAX_ENRICHMENT_ATTEMPTS``. After that it stays
  ``error`` and ``status``/``doctor`` report it as failed, so no outage strands a recording
  silently.
* **Stale results.** If the CAS misses, content changed mid-flight: the result is dropped, no
  event is appended, and the doc is already ``pending`` again from the content write.
* **One event per content hash.** The outbox dedupes on ``(recording_id, content_hash)``, so a
  taxonomy re-classification of unchanged content re-enriches but emits no second event.
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
    PATH_ENRICHMENT_STATUS,
    MongoSink,
)

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
        {"$set": {PATH_ENRICHMENT_STATUS: ENRICHMENT_PENDING}},
    )
    return int(res.modified_count)


def _file_paths(files: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for slot, ref in (files or {}).items():
        if isinstance(ref, dict) and ref.get("rel_path"):
            out[slot] = ref["rel_path"]
    return out


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
        self._retry: dict[str, tuple[float, int]] = {}  # id -> (next_attempt, failures)

    # -- scheduling ----------------------------------------------------------------------

    def _defer(self, rid: str, exc: EnrichmentUnavailable) -> None:
        with self._lock:
            failures = self._retry.get(rid, (0.0, 0))[1] + 1
            delay = min(self.backoff_base * 2 ** (failures - 1), self.backoff_max)
            try:
                ra = float(exc.retry_after) if exc.retry_after is not None else None
            except (TypeError, ValueError):
                ra = None
            if ra is not None and ra >= 0:
                delay = ra
            self._retry[rid] = (self._clock() + delay, failures)

    def _next_attempt(self, d: dict[str, Any]) -> float | None:
        """When to retry a doc that just failed; None once the attempt cap is reached."""
        attempts = int((d.get("enrichment") or {}).get("attempts") or 0) + 1
        if attempts >= MAX_ENRICHMENT_ATTEMPTS:
            return None
        return self._wall() + min(ERROR_RETRY_BASE * 2 ** (attempts - 1), ERROR_RETRY_MAX)

    def _due(self, rid: str) -> bool:
        with self._lock:
            entry = self._retry.get(rid)
        return entry is None or self._clock() >= entry[0]

    # -- one doc -------------------------------------------------------------------------

    def reclassify_stale(self, current_taxonomy_version: str | None = None) -> int:
        """Re-queue docs classified under another taxonomy (c44); serve calls this at start."""
        version = current_taxonomy_version or self.gateway.taxonomy.version
        return mark_for_reclassification(self.store, version)

    def _build_payload(self, d: dict[str, Any]) -> dict[str, Any]:
        summary = (d.get("summary") or "").strip()
        chunks = chunk_segments(d.get("segments") or [])
        texts = ([summary] if summary else []) + [c.text for c in chunks]
        payload: dict[str, Any] = {
            "status": ENRICHMENT_DONE,
            "model": EMBED_MODEL,
            "dims": EMBED_DIMS,
            "labels": [],
            "filters": {},
            "chunks": [],
            "enriched_at": _now_iso(),
            "error": None,
        }
        if not texts:
            return payload
        vectors = self.gateway.embed(texts)
        if summary:
            payload["summary_vector"] = vectors[0]
            vectors = vectors[1:]
        payload["chunks"] = [
            {"index": c.index, "start": c.start, "end": c.end, "text": c.text, "vector": v}
            for c, v in zip(chunks, vectors)
        ]
        body = "\n\n".join(([summary] if summary else []) + [c.text for c in chunks])
        cls = self.gateway.classify(body[:CLASSIFY_MAX_CHARS])
        payload.update(
            labels=list(cls.labels),
            filters=dict(cls.filters),
            taxonomy_version=cls.taxonomy_version,
        )
        return payload

    def _process(self, d: dict[str, Any]) -> str:
        rid, chash = d["_id"], d["content_hash"]
        try:
            payload = self._build_payload(d)
        except EnrichmentUnavailable as exc:
            self._defer(rid, exc)
            return "unavailable"
        except CliError as exc:
            self.store.set_enrichment_error(rid, chash, exc.message, self._next_attempt(d))
            return "errors"
        if not self.store.set_enrichment(rid, chash, payload):
            return "stale"
        with self._lock:
            self._retry.pop(rid, None)
            self.outbox.append(
                rid,
                chash,
                mongo={"collection": vindex.COLLECTION, "doc_id": rid},
                files=_file_paths(d.get("files")),
                labels=payload["labels"],
                taxonomy_version=payload.get("taxonomy_version"),
                model=EMBED_MODEL,
            )
        return "done"

    # -- driving -------------------------------------------------------------------------

    def run_once(self) -> dict[str, int]:
        """Process up to ``batch`` due pending docs, at most ``concurrency`` at a time."""
        stats = dict.fromkeys(("done", "stale", "unavailable", "errors", "deferred"), 0)
        # Over-fetch so backed-off docs at the head of the queue don't starve the rest.
        picked = self.store.pending_enrichment(self.batch + len(self._retry), now=self._wall())
        todo = []
        for d in picked:
            if not self._due(d["_id"]):
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
