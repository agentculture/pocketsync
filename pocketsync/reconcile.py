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
#: Version of what a landing writes (document shape, file layout, content-hash definition).
#: Bump it whenever an upgrade changes that, so the next pass re-lands every recording once.
#: 1: first release (0.11.0, hash excludes ``raw``).
LANDING_SCHEMA = 1
CATCH_UP_INITIAL, CATCH_UP_UPGRADE = "initial", "upgrade"
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


def _same_instant(stored: str | None, listed: str | None) -> bool:
    """True only when both timestamps exist and name the same instant (format-insensitive).

    A missing ``updated_at`` can never prove "unchanged", so it always re-lands.
    """
    if not stored or not listed:
        return False
    if stored == listed:
        return True
    a, b = _parse_ts(stored), _parse_ts(listed)
    return a is not None and a == b


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
    catch_up: str | None = None  # "initial" | "upgrade" when the pass was a catch-up
    planned: list[str] = field(default_factory=list)

    def stats(self) -> dict[str, Any]:
        return {
            k: getattr(self, k)
            for k in ("listed", "landed", "skipped", "missed", "errors", "pages", "ok", "catch_up")
        }


@dataclass(frozen=True)
class ReconcilePolicy:
    """Paging, overlap and backoff knobs for :class:`Reconciler` (defaults fit ~50 calls/min)."""

    overlap_days: int = DEFAULT_OVERLAP_DAYS
    page_limit: int = MAX_PAGE_LIMIT
    page_delay: float = DEFAULT_PAGE_DELAY
    grace: float = DEFAULT_GRACE
    backoff_base: float = DEFAULT_BACKOFF_BASE
    backoff_cap: float = DEFAULT_BACKOFF_CAP
    max_retries: int = DEFAULT_MAX_RETRIES


class Reconciler:
    """Pages Pocket's list API and lands what the store does not already have."""

    def __init__(
        self,
        source: PocketSource,
        *,
        files_sink: Sink,
        store_sink: Sink,
        state: StateStore,
        policy: ReconcilePolicy | None = None,
        dry_run: bool = False,
        with_audio: bool = True,
        initial_since: str | date | datetime | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.source = source
        self.files_sink = files_sink
        self.store_sink = store_sink
        self.state = state
        p = policy or ReconcilePolicy()
        self.overlap_days = p.overlap_days
        self.page_limit = min(p.page_limit, MAX_PAGE_LIMIT)
        self.page_delay = p.page_delay
        self.grace = p.grace
        self.backoff_base = p.backoff_base
        self.backoff_cap = p.backoff_cap
        self.max_retries = p.max_retries
        self.dry_run = dry_run
        self.with_audio = with_audio
        self.initial_since = initial_since
        self._sleep = sleep
        self._clock = clock

    # -- public API -------------------------------------------------------------------

    def reconcile(self, since: str | date | datetime | None = None) -> ReconcileResult:
        """One pass to today (UTC); catches up automatically when ``initial_since`` is set.

        * Incremental (the normal case): from ``(since or watermark) - overlap``. Downtime of
          any length is covered, because the watermark only advances after a clean pass.
        * ``initial`` catch-up: no watermark yet (a fresh install). Backfills from
          ``initial_since``.
        * ``upgrade`` catch-up: the stored landing schema is older than
          :data:`LANDING_SCHEMA`. Re-lands every recording from ``initial_since``, even if
          unchanged in Pocket.

        A failed pass advances neither the watermark nor the schema, so a catch-up is retried
        by the next pass.
        """
        started = self._clock()
        catch_up = None if since is not None else self._catch_up()
        if catch_up is not None:
            start_day = _parse_since(self.initial_since).date()
        else:
            base = (
                _parse_since(since)
                if since is not None
                else (_parse_ts(self.state.get_watermark()) or started)
            )
            start_day = (base - timedelta(days=self.overlap_days)).date()
        result = self._pass(
            start_day,
            started,
            count_missed=catch_up is None,
            where="reconcile" if catch_up is None else f"catch-up:{catch_up}",
            force=catch_up == CATCH_UP_UPGRADE,
        )
        result.catch_up = catch_up
        if result.ok and not self.dry_run:
            self.state.set_watermark(_iso(started))
            self.state.set_landing_schema(LANDING_SCHEMA)
        return result

    def _catch_up(self) -> str | None:
        if self.initial_since is None:
            return None
        if self.state.get_watermark() is None:
            return CATCH_UP_INITIAL
        if self.state.get_landing_schema() != LANDING_SCHEMA:
            return CATCH_UP_UPGRADE
        return None

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
        self,
        start_day: date,
        started: datetime,
        *,
        count_missed: bool,
        where: str,
        force: bool = False,
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
                    self._handle(
                        item, started, result, count_missed and not self.dry_run, where, force
                    )
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

    def _handle(
        self,
        item,
        now: datetime,
        result: ReconcileResult,
        count: bool,
        where: str,
        force: bool = False,
    ):
        result.listed += 1
        exists, stored_updated = self._stored(item.id)
        if (
            not force
            and exists
            and _same_instant(stored_updated, item.updated_at)
            and not self._audio_wanted(item.id)
        ):
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
                    with_audio=self.with_audio,
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
        if landing.audio_error and not self.dry_run:
            self.state.record_error(where, f"{item.id}: audio: {landing.audio_error}")
        if count and not exists and self._older_than_grace(item, now):
            result.missed += 1
            self.state.increment(COUNTER_WEBHOOK_MISSED)

    def _audio_wanted(self, recording_id: str) -> bool:
        """Audio is wanted and the files sink does not have it yet (retry missing audio)."""
        if not self.with_audio:
            return False
        fstate = self.files_sink.get_state(recording_id)
        return not (fstate and (fstate.has_audio or fstate.audio_absent))

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
