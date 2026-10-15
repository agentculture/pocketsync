"""``pocketsync serve``: receiver, landing consumer, reconciler and worker in one process.

:class:`Service` owns the threads and is testable without signals (``start()`` / ``stop()``);
the CLI verb (:mod:`pocketsync.cli._commands.serve`) builds it from Settings and wires
SIGTERM/SIGINT to its shared :attr:`Service.stop_event`.

Threads (all daemon, all stop on ``stop_event``):

* the :class:`~pocketsync.webhook.server.WebhookServer` (loopback only) verifies, dedupes and
  enqueues onto an **unbounded** ``queue.Queue`` -- the receiver marks a dedupe key *before*
  enqueueing, so a bounded queue that filled up would silently drop events;
* ``landing`` drains the queue and routes by event type: ``recording.deleted`` ->
  :func:`~pocketsync.sync.handle_deleted`; ``recording.merged`` ->
  :func:`~pocketsync.sync.handle_merged` (ids from the payload, see :func:`merge_ids`) and a
  re-land of the survivor; anything else -> :func:`~pocketsync.sync.land`;
* ``reconciler`` runs :meth:`~pocketsync.reconcile.Reconciler.run_forever`;
* ``enrichment`` calls :meth:`~pocketsync.enrich.worker.EnrichmentWorker.run_once` every
  ``enrich_interval`` seconds;
* ``outbox`` drains ``events.jsonl`` to the broker every ``drain_interval`` seconds.

Shutdown order: stop the HTTP server (no new events), set ``stop_event``, let the landing
consumer finish what is already queued, then join every thread within ``join_timeout``.
Errors inside a loop are recorded in the state store (``where="serve"``) and never kill the
loop.
"""

from __future__ import annotations

import queue as queue_mod
import threading
import time
from typing import Any, Callable

from pocketsync.cli._errors import EXIT_ENV_ERROR, CliError
from pocketsync.sync import handle_deleted, handle_merged, land
from pocketsync.webhook.server import WebhookServer

EVENT_DELETED = "recording.deleted"
EVENT_MERGED = "recording.merged"
DEFAULT_ENRICH_INTERVAL = 5.0
DEFAULT_DRAIN_INTERVAL = 30.0
DEFAULT_JOIN_TIMEOUT = 15.0
_POLL = 0.2

# Pocket's merge payload shape is undocumented; these are the keys we look for.
_DST_KEYS = ("merged_into", "merged_into_id", "target_recording_id", "into")
_SRC_KEYS = ("merged_recording_ids", "source_recording_ids", "merged_from", "merged_ids")


def _ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        rid = item.get("id") if isinstance(item, dict) else item
        if rid not in (None, ""):
            out.append(str(rid))
    return out


def merge_ids(recording_id: str, payload: dict[str, Any]) -> list[tuple[str, str]]:
    """``[(src_id, dst_id), ...]`` for a ``recording.merged`` event, or ``[]`` if unknown.

    Accepts either "this recording was merged into X" (``merged_into`` and friends, at the
    payload top level or under ``recording``) or "these recordings were merged into this one"
    (``merged_recording_ids`` / ``merged_from`` / ..., ids or ``{id}`` objects).
    """
    rec = payload.get("recording")
    containers = [payload] + ([rec] if isinstance(rec, dict) else [])
    for c in containers:
        for key in _DST_KEYS:
            dst = c.get(key)
            if isinstance(dst, dict):
                dst = dst.get("id")
            if dst not in (None, "") and str(dst) != recording_id:
                return [(recording_id, str(dst))]
    for c in containers:
        for key in _SRC_KEYS:
            srcs = [s for s in _ids(c.get(key)) if s != recording_id]
            if srcs:
                return [(s, recording_id) for s in srcs]
    return []


class Service:
    """All of ``serve`` in one object. ``reconciler``/``worker``/``publisher`` are optional."""

    #: Seconds of consumer idleness between retries of failed inbox entries.
    inbox_retry_interval: float = 300.0

    def __init__(
        self,
        source: Any,
        *,
        files_sink: Any,
        store_sink: Any,
        state: Any,
        secret: str,
        port: int = 8787,
        host: str = "127.0.0.1",
        reconciler: Any = None,
        worker: Any = None,
        publisher: Any = None,
        reconcile_interval: float = 300.0,
        enrich_interval: float = DEFAULT_ENRICH_INTERVAL,
        drain_interval: float = DEFAULT_DRAIN_INTERVAL,
        join_timeout: float = DEFAULT_JOIN_TIMEOUT,
        stop_event: threading.Event | None = None,
        server_factory: Callable[..., Any] = WebhookServer,
    ) -> None:
        self.source = source
        self.files_sink = files_sink
        self.store_sink = store_sink
        self.state = state
        self.reconciler = reconciler
        self.worker = worker
        self.publisher = publisher
        self.reconcile_interval = reconcile_interval
        self.enrich_interval = enrich_interval
        self.drain_interval = drain_interval
        self.join_timeout = join_timeout
        self.stop_event = stop_event or threading.Event()
        self.queue: queue_mod.Queue[Any] = queue_mod.Queue()  # unbounded on purpose
        self.server = server_factory(host, port, secret=secret, state=state, queue=self.queue)
        self.threads: list[threading.Thread] = []
        self.stats = dict.fromkeys(("landed", "deleted", "merged", "errors"), 0)
        self._lock = threading.Lock()
        self._started = False

    # -- lifecycle -----------------------------------------------------------------------

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def thread_names(self) -> list[str]:
        return [t.name for t in self.threads]

    def start(self) -> None:
        try:
            self.server.start()
        except OSError as exc:
            raise CliError(
                EXIT_ENV_ERROR,
                f"cannot bind the webhook receiver on {self.server.host}:{self.server.port}: "
                f"{exc.strerror or exc}",
                "Stop the other process on that port or pass a different --port.",
            ) from exc
        self._started = True
        self._spawn("landing", self._landing_loop)
        if self.reconciler is not None:
            self._spawn(
                "reconciler",
                lambda: self.reconciler.run_forever(self.reconcile_interval, self.stop_event),
            )
        if self.worker is not None:
            reclassify = getattr(self.worker, "reclassify_stale", None)
            if reclassify is not None:
                try:
                    reclassify()
                except Exception as exc:  # a taxonomy re-queue must not block serving
                    self.state.record_error("enrichment", f"reclassify on start: {exc}")
            self._spawn(
                "enrichment", lambda: self._every(self.enrich_interval, self.worker.run_once)
            )
        if self.publisher is not None:
            self._spawn("outbox", lambda: self._every(self.drain_interval, self.publisher.drain))

    def stop(self, timeout: float | None = None) -> list[str]:
        """Stop everything; return the names of threads still alive after the join timeout."""
        if self._started:
            self.server.stop()
            self._started = False
        self.stop_event.set()
        deadline = time.monotonic() + (self.join_timeout if timeout is None else timeout)
        for t in self.threads:
            t.join(max(0.0, deadline - time.monotonic()))
        return [t.name for t in self.threads if t.is_alive()]

    def _spawn(self, name: str, target: Callable[[], Any]) -> None:
        t = threading.Thread(target=target, name=name, daemon=True)
        self.threads.append(t)
        t.start()

    def _every(self, interval: float, fn: Callable[[], Any]) -> None:
        while not self.stop_event.is_set():
            try:
                fn()
            except Exception as exc:  # keep the daemon alive; the error is on record
                self._error(f"{getattr(fn, '__name__', 'loop')}: {exc}")
            if self.stop_event.wait(interval):
                break

    # -- landing consumer ----------------------------------------------------------------

    def _landing_loop(self) -> None:
        self.process_pending()
        last_sweep = time.monotonic()
        while True:
            try:
                item = self.queue.get(timeout=_POLL)
            except queue_mod.Empty:
                if self.stop_event.is_set():
                    return
                if time.monotonic() - last_sweep >= self.inbox_retry_interval:
                    self.process_pending()
                    last_sweep = time.monotonic()
                continue
            self._consume(item)

    def _consume(self, item: tuple[Any, ...]) -> None:
        rid, event_type, payload, *rest = item
        key = rest[0] if rest else None
        ok = self.handle(rid, event_type, payload or {})
        if key is not None:
            self._settle(key, ok)

    def _settle(self, key: str, ok: bool) -> None:
        try:
            if ok:
                self.state.inbox_done(key)
            else:
                self.state.inbox_fail(key, "handler failed; see errors")
        except Exception as exc:  # the entry stays pending and is replayed later
            self._error(f"inbox {key}: {exc}")

    def process_pending(self) -> int:
        """Handle every pending inbox entry (acknowledged, never handled); return successes.

        Runs when the landing consumer starts (replaying what a previous process lost) and
        again whenever it has been idle for ``inbox_retry_interval``. Handlers are idempotent,
        so an entry that is also still queued is harmless.
        """
        try:
            pending = self.state.inbox_pending()
        except Exception as exc:
            self._error(f"inbox replay: {exc}")
            return 0
        done = 0
        for entry in pending:
            item = entry["item"]
            ok = self.handle(
                item.get("recording_id"), item.get("event_type", ""), item.get("payload") or {}
            )
            self._settle(entry["key"], ok)
            done += ok
        return done

    def _error(self, message: str) -> None:
        with self._lock:
            self.stats["errors"] += 1
        try:
            self.state.record_error("serve", message)
        except Exception:  # nosec B110
            pass  # the state store itself is down; there is nowhere to record this

    def _bump(self, key: str) -> None:
        with self._lock:
            self.stats[key] += 1

    def _land(self, rid: str) -> None:
        landing = land(rid, self.source, files_sink=self.files_sink, store_sink=self.store_sink)
        self._bump("landed")
        if landing.audio_error:
            self._error(f"{rid}: audio: {landing.audio_error}")

    def handle(self, rid: str | None, event_type: str, payload: dict[str, Any]) -> bool:
        """Route one webhook event; True when handled. Never raises: failures are recorded."""
        if not rid:
            self._error(f"{event_type}: event carried no recording id")
            return False
        sinks = {"files_sink": self.files_sink, "store_sink": self.store_sink}
        try:
            if event_type == EVENT_DELETED:
                handle_deleted(rid, **sinks)
                self._bump("deleted")
            elif event_type == EVENT_MERGED:
                pairs = merge_ids(rid, payload)
                if not pairs:
                    self._error(
                        f"{rid}: recording.merged carried no merge ids; "
                        "re-landing it and leaving the rest to the reconciler"
                    )
                    self._land(rid)
                    return True
                for src, dst in pairs:
                    handle_merged(src, dst, **sinks)
                    self._bump("merged")
                for dst in sorted({d for _, d in pairs}):
                    self._land(dst)
            else:
                self._land(rid)
        except CliError as exc:
            self._error(f"{rid}: {event_type}: {exc.message}")
            return False
        except Exception as exc:  # a bug or a backend outage must not kill the consumer
            self._error(f"{rid}: {event_type}: {type(exc).__name__}: {exc}")
            return False
        return True
