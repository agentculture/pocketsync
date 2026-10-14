"""Reconciler and backfill: the safety net behind the webhook path.

Webhooks are best effort, so on an interval the reconciler pages ``list_recordings`` from
``watermark - 2 days`` (the overlap absorbs clock skew and late edits) and lands anything new
or changed through the same :func:`pocketsync.sync.land` the webhook handler uses. It appends
no events itself: a landing only leaves ``enrichment.status = pending`` for the worker.

Rules worth knowing:

* **Change detection costs no extra API calls.** A list item is landed only if the store has no
  doc for it, or the doc's ``source.updated_at`` (Pocket's timestamp, kept by the Mongo sink)
  differs from the listed ``updated_at``. ``land`` is idempotent by content hash, so a false
  positive costs one API call and no write; an unchanged pass costs only the list calls.
* **webhook_missed.** A recording the store had no doc for, found by :meth:`Reconciler.reconcile`
  and older than ``grace`` seconds (default 600, measured from its ``created_at`` /
  ``recording_at``), means no webhook delivered it: ``sync_state.counters.webhook_missed`` is
  incremented. Younger recordings are landed but not counted (the webhook may still arrive).
  Backfill and dry runs never count.
* **Backoff.** :class:`RateLimited` sleeps ``Retry-After`` if the server gave one, else
  ``backoff_base * 2**attempt`` seconds, capped at ``backoff_cap``, for at most ``max_retries``
  retries per call. A politeness ``page_delay`` separates pages. Rate limits are unknown, so
  the defaults are conservative.
* **Watermark.** Advanced to the pass's start time only after every page landed without error;
  on failure the error is recorded and the watermark stays, so the next pass re-covers it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from pocketsync.cli._errors import EXIT_USER_ERROR, CliError
from pocketsync.pocket.base import MAX_PAGE_LIMIT, PocketSource, RateLimited
from pocketsync.sinks.base import Sink
from pocketsync.state import COUNTER_WEBHOOK_MISSED, StateStore
from pocketsync.sync import land

DEFAULT_INTERVAL = 300.0
DEFAULT_OVERLAP_DAYS = 2
DEFAULT_PAGE_DELAY = 0.5
DEFAULT_GRACE = 600.0
DEFAULT_BACKOFF_BASE = 2.0
DEFAULT_BACKOFF_CAP = 120.0
DEFAULT_MAX_RETRIES = 5

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(_TS_FORMAT)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text.split("~")[0])
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_since(since: str | date | datetime) -> datetime:
    if isinstance(since, datetime):
        return since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    if isinstance(since, date):
        return datetime(since.year, since.month, since.day, tzinfo=timezone.utc)
    dt = _parse_ts(str(since))
    if dt is None:
        raise CliError(
            EXIT_USER_ERROR,
            f"invalid date: {since!r}",
            "Use YYYY-MM-DD or an ISO-8601 timestamp.",
        )
    return dt


@dataclass
class ReconcileResult:
    """Outcome of one pass. ``ok`` is False if any page or recording failed."""

    listed: int = 0
    landed: int = 0
    skipped: int = 0
    missed: int = 0
    errors: int = 0
    pages: int = 0
    ok: bool = True
    planned: list[str] = field(default_factory=list)

    def stats(self) -> dict[str, Any]:
        return {
            k: getattr(self, k)
            for k in ("listed", "landed", "skipped", "missed", "errors", "pages", "ok")
        }


class Reconciler:
    """Pages Pocket's list API and lands what the store does not already have."""

    def __init__(
        self,
        source: PocketSource,
        *,
        files_sink: Sink,
        store_sink: Sink,
        state: StateStore,
        overlap_days: int = DEFAULT_OVERLAP_DAYS,
        page_limit: int = MAX_PAGE_LIMIT,
        page_delay: float = DEFAULT_PAGE_DELAY,
        grace: float = DEFAULT_GRACE,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        backoff_cap: float = DEFAULT_BACKOFF_CAP,
        max_retries: int = DEFAULT_MAX_RETRIES,
        dry_run: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.source = source
        self.files_sink = files_sink
        self.store_sink = store_sink
        self.state = state
        self.overlap_days = overlap_days
        self.page_limit = min(page_limit, MAX_PAGE_LIMIT)
        self.page_delay = page_delay
        self.grace = grace
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.max_retries = max_retries
        self.dry_run = dry_run
        self._sleep = sleep
        self._clock = clock

    # -- public API -------------------------------------------------------------------

    def reconcile(self, since: str | date | datetime | None = None) -> ReconcileResult:
        """One incremental pass from ``(since or watermark) - overlap`` to today (UTC)."""
        started = self._clock()
        base = (
            _parse_since(since)
            if since is not None
            else (_parse_ts(self.state.get_watermark()) or started)
        )
        start_day = (base - timedelta(days=self.overlap_days)).date()
        result = self._pass(start_day, started, count_missed=True, where="reconcile")
        if result.ok and not self.dry_run:
            self.state.set_watermark(_iso(started))
        return result

    def backfill(self, since: str | date | datetime) -> ReconcileResult:
        """Land everything from ``since`` (exact day, no overlap) to today. Watermark untouched."""
        started = self._clock()
        start_day = _parse_since(since).date()
        return self._pass(start_day, started, count_missed=False, where="backfill")

    def run_forever(
        self, interval: float = DEFAULT_INTERVAL, stop_event: threading.Event | None = None
    ) -> None:
        """Reconcile every ``interval`` seconds until ``stop_event`` is set."""
        stop = stop_event or threading.Event()
        while not stop.is_set():
            try:
                self.reconcile()
            except Exception as exc:  # keep the daemon alive; the error is on record
                self.state.record_error("reconcile", str(exc))
            if stop.wait(interval):
                break

    # -- internals --------------------------------------------------------------------

    def _pass(
        self, start_day: date, started: datetime, *, count_missed: bool, where: str
    ) -> ReconcileResult:
        result = ReconcileResult()
        start_s, end_s = start_day.isoformat(), started.astimezone(timezone.utc).date().isoformat()
        page = 1
        try:
            while True:
                listing = self._backoff(
                    lambda p=page: self.source.list_recordings(
                        start_date=start_s, end_date=end_s, page=p, limit=self.page_limit
                    )
                )
                result.pages += 1
                for item in listing.recordings:
                    self._handle(item, started, result, count_missed and not self.dry_run, where)
                if not listing.has_more:
                    break
                page += 1
                if self.page_delay:
                    self._sleep(self.page_delay)
        except CliError as exc:
            result.ok = False
            result.errors += 1
            self.state.record_error(where, exc.message)
        result.ok = result.ok and result.errors == 0
        if not self.dry_run:
            self.state.record_run(_iso(started), _iso(self._clock()), result.stats())
        return result

    def _handle(self, item, now: datetime, result: ReconcileResult, count: bool, where: str):
        result.listed += 1
        exists, stored_updated = self._stored(item.id)
        if exists and stored_updated == item.updated_at:
            result.skipped += 1
            return
        try:
            landing = self._backoff(
                lambda: land(
                    item.id,
                    self.source,
                    files_sink=self.files_sink,
                    store_sink=self.store_sink,
                    dry_run=self.dry_run,
                )
            )
        except RateLimited:
            raise
        except CliError as exc:
            result.errors += 1
            self.state.record_error(where, f"{item.id}: {exc.message}")
            return
        result.landed += 1
        result.planned.extend(landing.planned)
        if count and not exists and self._older_than_grace(item, now):
            result.missed += 1
            self.state.increment(COUNTER_WEBHOOK_MISSED)

    def _stored(self, recording_id: str) -> tuple[bool, str | None]:
        """(has a doc, its Pocket ``source.updated_at``) without any Pocket call."""
        collection = getattr(self.store_sink, "collection", None)
        if collection is not None:
            doc = collection.find_one({"_id": recording_id}, {"source": 1})
            if doc is None:
                return False, None
            return True, (doc.get("source") or {}).get("updated_at")
        return self.store_sink.get_state(recording_id) is not None, None

    def _older_than_grace(self, item, now: datetime) -> bool:
        born = _parse_ts(item.created_at) or _parse_ts(item.recording_at)
        return born is None or (now - born).total_seconds() > self.grace

    def _backoff(self, fn):
        attempt = 0
        while True:
            try:
                return fn()
            except RateLimited as exc:
                if attempt >= self.max_retries:
                    raise
                if exc.retry_after is not None:
                    delay = exc.retry_after
                else:
                    delay = min(self.backoff_base * 2**attempt, self.backoff_cap)
                self._sleep(delay)
                attempt += 1
