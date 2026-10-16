"""MongoDB sink: one document per recording in :data:`pocketsync.vindex.COLLECTION`.

pymongo is the optional extra ``pocketsync[mongo]`` and is imported lazily, only by
:meth:`MongoSink.from_uri`; the class itself works on any injected pymongo-compatible
database (tests use mongomock). The connection URI is never logged or kept, because it
can embed a password.

Document shape (``_id`` = recording id)::

    title, description, recording_at, duration, language, state, folder_id, tags,
    speakers, segments[], summary, action_items[]      # content field group
    source {created_at, updated_at}                     # Pocket's own timestamps
    content_hash, files {FileSet}, landed_at, updated_at
    deleted_in_pocket, deleted_at, merged_into, merged_at
    enrichment {status: pending|done|error, model, dims, labels, filters,
                taxonomy_version, summary_vector, chunks, enriched_at, error, attempts,
                next_attempt_at, mode, generation, deferred_reason}

``updated_at`` is pocketsync's write time and moves only when content changes.
``Recording.raw`` and ``fetched_at`` are *not* stored (bulky, volatile wire data).

Field groups: a content write ``$set``s only the content group plus ``content_hash`` /
``updated_at`` / ``files`` and resets enrichment to ``pending`` while ``$unset``-ing every
derived enrichment field (vectors, labels, model, ...) so stale vectors never describe new
content. :meth:`MongoSink.set_enrichment` writes only ``enrichment.*`` and only while the
stored ``content_hash`` still equals the hash the enrichment was computed from (CAS).

Requeue and generations: :meth:`MongoSink.requeue` flips docs back to ``pending`` with a
``mode`` (``full|classify|embed``) and increments ``enrichment.generation``. The worker reads
the generation at pick-up (:meth:`MongoSink.pending_enrichment` always reports it, ``0`` for a
doc never requeued) and passes it back as ``expected_generation``, so a requeue that lands while
the worker holds the doc makes the finishing write miss and the doc stays ``pending``. The
generation is monotonic: a content change resets status/mode/deferral but never rewinds it.
:meth:`MongoSink.set_deferred` persists why a pending doc is waiting (``deferred_reason``) and
when it is due (``next_attempt_at``); ``pending_enrichment(now=...)`` skips it until then.
Nothing in this module deletes documents.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from pocketsync import vindex
from pocketsync.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from pocketsync.model import Recording
from pocketsync.sinks.base import FileSet, PutResult, RecordState

DEFAULT_DB = "pocketsync"

ENRICHMENT_PENDING = "pending"
ENRICHMENT_DONE = "done"
ENRICHMENT_ERROR = "error"
PATH_ENRICHMENT_STATUS = "enrichment.status"
PATH_ENRICHMENT_ATTEMPTS = "enrichment.attempts"
PATH_NEXT_ATTEMPT = "enrichment.next_attempt_at"
PATH_GENERATION = "enrichment.generation"
PATH_MODE = "enrichment.mode"
PATH_DEFERRED_REASON = "enrichment.deferred_reason"
#: Requeue modes: ``full`` (embed + classify), ``classify`` (labels only, keep vectors),
#: ``embed`` (vectors only, keep labels). A doc without ``enrichment.mode`` is ``full``.
MODE_FULL = "full"
MODE_CLASSIFY = "classify"
MODE_EMBED = "embed"
ENRICHMENT_MODES = (MODE_FULL, MODE_CLASSIFY, MODE_EMBED)
#: An ``error`` doc is retried automatically (on a backoff, see the worker) until it has failed
#: this many times; after that it stays ``error`` and ``status``/``doctor`` report it as failed.
MAX_ENRICHMENT_ATTEMPTS = 10
_UNSET = "$unset"

# Fields set_enrichment may write (under ``enrichment.``).
ENRICHMENT_FIELDS = frozenset(
    {
        "status",
        "model",
        "dims",
        "labels",
        "filters",
        "taxonomy_version",
        "summary_vector",
        "chunks",
        "enriched_at",
        "error",
        "attempts",
        "next_attempt_at",
        "mode",
        "generation",
        "deferred_reason",
    }
)
# Dropped whenever content changes. ``generation`` is kept: it only ever moves forward.
_DERIVED = ENRICHMENT_FIELDS - {"status", "attempts", "generation"}

_CONTENT_FIELDS = (
    "title",
    "description",
    "recording_at",
    "duration",
    "language",
    "state",
    "folder_id",
    "tags",
    "speakers",
    "segments",
    "summary",
    "action_items",
)

_INSTALL_HINT = "Install the Mongo extra: pip install 'pocketsync[mongo]'"
DUPLICATE_KEY = 11000


def _cas_filter(
    recording_id: str, expected_hash: str, expected_generation: int | None
) -> dict[str, Any]:
    """CAS match on content hash and, when given, the generation claimed at pick-up.

    ``expected_generation=None`` skips the generation check (pre-generation callers);
    ``0`` also matches a doc that has no generation field yet.
    """
    flt: dict[str, Any] = {"_id": recording_id, "content_hash": expected_hash}
    if expected_generation is not None:
        gen = int(expected_generation)
        flt[PATH_GENERATION] = {"$in": [0, None]} if gen == 0 else gen
    return flt


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_duplicate_key(exc: BaseException) -> bool:
    """True for pymongo's DuplicateKeyError (or a compatible stub) without importing pymongo."""
    return getattr(exc, "code", None) == DUPLICATE_KEY or type(exc).__name__ == "DuplicateKeyError"


def connect(uri: str, db: str = DEFAULT_DB, **client_kwargs: Any) -> Any:
    """Return a pymongo database for ``uri``; lazily imports pymongo. Never echoes the URI."""
    try:
        import pymongo
    except ImportError as exc:
        raise CliError(EXIT_ENV_ERROR, "pymongo is not installed", _INSTALL_HINT) from exc
    return pymongo.MongoClient(uri, **client_kwargs)[db]


def content_doc(rec: Recording) -> dict[str, Any]:
    """The content field group of ``rec`` as stored in the document."""
    d = rec.to_dict()
    out = {k: d[k] for k in _CONTENT_FIELDS}
    out["source"] = {"created_at": rec.created_at, "updated_at": rec.updated_at}
    return out


class MongoSink:
    """A :class:`~pocketsync.sinks.base.Sink` backed by one Mongo collection."""

    def __init__(self, database: Any, *, now: Callable[[], str] | None = None) -> None:
        self._db = database
        self._col = database[vindex.COLLECTION]
        self._now = now or _now

    @classmethod
    def from_uri(
        cls, uri: str, db: str = DEFAULT_DB, *, now: Callable[[], str] | None = None, **kw: Any
    ) -> MongoSink:
        return cls(connect(uri, db, **kw), now=now)

    def __repr__(self) -> str:
        return f"MongoSink(collection={vindex.COLLECTION!r})"

    @property
    def collection(self) -> Any:
        return self._col

    def ensure_indexes(self) -> None:
        """Create the regular (non-vector) indexes; idempotent. Vector indexes: vindex.py."""
        for path in (PATH_ENRICHMENT_STATUS, vindex.PATH_DELETED, vindex.PATH_RECORDING_AT):
            self._col.create_index(path)

    # -- Sink protocol -------------------------------------------------------------------

    def put_recording(
        self,
        rec: Recording,
        files: FileSet | None = None,
        *,
        dry_run: bool = False,
        audio_url: str | None = None,
    ) -> PutResult:
        del audio_url  # Sink protocol parameter; Mongo stores file refs, never audio bytes
        chash = rec.content_hash()
        locs = {"collection": vindex.COLLECTION, "id": rec.id}
        if dry_run:
            st = self.get_state(rec.id)
            changed = st is None or st.content_hash != chash
            return PutResult(changed, chash, locs, files or FileSet())

        now = self._now()
        update: dict[str, Any] = {
            "$set": {
                **content_doc(rec),
                "content_hash": chash,
                "updated_at": now,
                PATH_ENRICHMENT_STATUS: ENRICHMENT_PENDING,
                PATH_ENRICHMENT_ATTEMPTS: 0,
            },
            _UNSET: {f"enrichment.{k}": "" for k in sorted(_DERIVED)},
            "$setOnInsert": {
                "landed_at": now,
                vindex.PATH_DELETED: False,
                "deleted_at": None,
                "merged_into": None,
                "merged_at": None,
            },
        }
        # files=None keeps whatever refs are stored (an empty FileSet on first insert).
        fdict = (files or FileSet()).to_dict()
        update["$set" if files is not None else "$setOnInsert"]["files"] = fdict
        # Single conditional upsert: matches only if the stored hash differs. If a doc with
        # the same hash exists, the upsert's insert collides on _id -> identical content, no-op.
        try:
            res = self._col.update_one(
                {"_id": rec.id, "content_hash": {"$ne": chash}}, update, upsert=True
            )
        except Exception as exc:  # pymongo stays an optional import
            if not is_duplicate_key(exc):
                raise
            return self._unchanged(rec.id, chash, files, locs)
        changed = bool(res.upserted_id is not None or res.modified_count)
        if not changed:
            return self._unchanged(rec.id, chash, files, locs)
        return PutResult(True, chash, locs, files or FileSet())

    def _unchanged(
        self, rid: str, chash: str, files: FileSet | None, locs: dict[str, str]
    ) -> PutResult:
        """Same content: optionally refresh ``files`` refs (no updated_at bump)."""
        if files is not None:
            fdict = files.to_dict()
            self._col.update_one(
                {"_id": rid, "content_hash": chash, "files": {"$ne": fdict}},
                {"$set": {"files": fdict}},
            )
        found = self._col.find_one({"_id": rid}, {"files": 1}) or {}
        return PutResult(False, chash, locs, FileSet.from_dict(found.get("files") or {}))

    def get_state(self, recording_id: str) -> RecordState | None:
        d = self._col.find_one(
            {"_id": recording_id}, {"content_hash": 1, vindex.PATH_DELETED: 1, "merged_into": 1}
        )
        if d is None:
            return None
        return RecordState(
            content_hash=d.get("content_hash", ""),
            deleted_in_pocket=bool(d.get(vindex.PATH_DELETED, False)),
            merged_into=d.get("merged_into"),
        )

    def _mark(self, recording_id: str, fields: dict[str, Any]) -> None:
        res = self._col.update_one({"_id": recording_id}, {"$set": fields})
        if not res.matched_count:
            raise CliError(
                EXIT_USER_ERROR,
                f"unknown recording: {recording_id}",
                "Sync the recording before tombstoning or merging it.",
            )

    def tombstone(self, recording_id: str, when: str) -> None:
        self._mark(recording_id, {vindex.PATH_DELETED: True, "deleted_at": when})

    def mark_merged(self, src_id: str, dst_id: str, when: str) -> None:
        self._mark(src_id, {"merged_into": dst_id, "merged_at": when})

    # -- Enrichment (worker side) ---------------------------------------------------------

    def set_enrichment(
        self,
        recording_id: str,
        expected_hash: str,
        payload: dict,
        *,
        expected_generation: int | None = None,
    ) -> bool:
        """CAS-write ``payload`` into ``enrichment.*``; False if content moved on (or no doc).

        With ``expected_generation`` the write also misses when the doc was requeued since it
        was picked up. Only keys in :data:`ENRICHMENT_FIELDS` are allowed (ValueError
        otherwise). Any persisted deferral (``deferred_reason``, and ``next_attempt_at`` unless
        in ``payload``) is cleared.
        """
        bad = set(payload) - ENRICHMENT_FIELDS
        if bad:
            raise ValueError(f"not enrichment fields: {sorted(bad)}")
        if not payload:
            return False
        fields = {f"enrichment.{k}": v for k, v in payload.items()}
        if payload.get("status") == ENRICHMENT_DONE:
            fields.setdefault(PATH_ENRICHMENT_ATTEMPTS, 0)  # the cap counts consecutive failures
        unset = {
            path: ""
            for key, path in (
                ("next_attempt_at", PATH_NEXT_ATTEMPT),  # a success clears any retry schedule
                ("deferred_reason", PATH_DEFERRED_REASON),
            )
            if key not in payload
        }
        update: dict[str, Any] = {"$set": fields}
        if unset:
            update[_UNSET] = unset
        res = self._col.update_one(
            _cas_filter(recording_id, expected_hash, expected_generation), update
        )
        return res.matched_count == 1

    def set_enrichment_error(
        self,
        recording_id: str,
        expected_hash: str,
        message: str,
        next_attempt_at: float | None = None,
        *,
        expected_generation: int | None = None,
    ) -> bool:
        """CAS-record an enrichment failure: status=error, error=message, attempts += 1.

        ``next_attempt_at`` (epoch seconds) schedules the automatic retry; None leaves it
        terminal (the attempt cap was reached). ``expected_generation``: as in
        :meth:`set_enrichment`. Clears any ``deferred_reason``.
        """
        fields: dict[str, Any] = {
            PATH_ENRICHMENT_STATUS: ENRICHMENT_ERROR,
            "enrichment.error": message,
        }
        unset = {PATH_DEFERRED_REASON: ""}
        update: dict[str, Any] = {
            "$set": fields,
            "$inc": {PATH_ENRICHMENT_ATTEMPTS: 1},
            _UNSET: unset,
        }
        if next_attempt_at is None:
            unset[PATH_NEXT_ATTEMPT] = ""
        else:
            fields[PATH_NEXT_ATTEMPT] = float(next_attempt_at)
        res = self._col.update_one(
            _cas_filter(recording_id, expected_hash, expected_generation), update
        )
        return res.matched_count == 1

    def set_deferred(
        self,
        recording_id: str,
        expected_hash: str,
        generation: int | None,
        reason: str,
        next_attempt_at: float,
    ) -> bool:
        """Persist why a ``pending`` doc is waiting and when it is due (epoch seconds).

        CAS on content hash and ``generation`` (as ``expected_generation`` in
        :meth:`set_enrichment`); only a doc still ``pending`` is deferred. Status and
        attempts are left alone. False if nothing matched.
        """
        flt = _cas_filter(recording_id, expected_hash, generation)
        flt[PATH_ENRICHMENT_STATUS] = ENRICHMENT_PENDING
        res = self._col.update_one(
            flt,
            {
                "$set": {
                    PATH_DEFERRED_REASON: str(reason),
                    PATH_NEXT_ATTEMPT: float(next_attempt_at),
                }
            },
        )
        return res.matched_count == 1

    def requeue(self, ids: Any, mode: str = MODE_FULL) -> int:
        """Re-queue ``ids`` for enrichment in ``mode``; returns how many docs were requeued.

        Sets status ``pending``, attempts 0 and ``mode``; increments ``generation`` (so an
        in-flight worker run on the old generation cannot overwrite it); clears ``error``,
        ``next_attempt_at`` and ``deferred_reason``. Docs deleted in Pocket and unknown ids
        are skipped. Raises ValueError for an unknown mode.
        """
        if mode not in ENRICHMENT_MODES:
            raise ValueError(
                f"unknown enrichment mode {mode!r}; expected one of {ENRICHMENT_MODES}"
            )
        id_list = list(dict.fromkeys(ids))
        if not id_list:
            return 0
        res = self._col.update_many(
            {"_id": {"$in": id_list}, vindex.PATH_DELETED: {"$ne": True}},
            {
                "$set": {
                    PATH_ENRICHMENT_STATUS: ENRICHMENT_PENDING,
                    PATH_ENRICHMENT_ATTEMPTS: 0,
                    PATH_MODE: mode,
                },
                "$inc": {PATH_GENERATION: 1},
                _UNSET: {
                    "enrichment.error": "",
                    PATH_NEXT_ATTEMPT: "",
                    PATH_DEFERRED_REASON: "",
                },
            },
        )
        return int(res.matched_count)

    def pending_enrichment(self, limit: int, now: float | None = None) -> list[dict[str, Any]]:
        """Docs awaiting enrichment (not deleted in Pocket), oldest first.

        The ``pending`` ones. With ``now`` (epoch seconds), a pending doc with a persisted
        deferral (``next_attempt_at`` in the future) is skipped until due, and the ``error``
        docs whose automatic retry is due and whose attempts are below the cap are included.
        Every returned doc carries ``enrichment.generation`` (``0`` when never requeued),
        the value to pass back as ``expected_generation``.
        """
        if limit <= 0:
            return []
        pending: dict[str, Any] = {PATH_ENRICHMENT_STATUS: ENRICHMENT_PENDING}
        due: list[dict[str, Any]] = [pending]
        if now is not None:
            pending["$or"] = [
                {PATH_NEXT_ATTEMPT: {"$lte": float(now)}},
                {PATH_NEXT_ATTEMPT: {"$exists": False}},
            ]
            due.append(
                {
                    PATH_ENRICHMENT_STATUS: ENRICHMENT_ERROR,
                    PATH_ENRICHMENT_ATTEMPTS: {"$lt": MAX_ENRICHMENT_ATTEMPTS},
                    # A doc that went to `error` before retries were scheduled (0.11.0) has
                    # no next_attempt_at: it is due now.
                    "$or": [
                        {PATH_NEXT_ATTEMPT: {"$lte": float(now)}},
                        {PATH_NEXT_ATTEMPT: {"$exists": False}},
                    ],
                }
            )
        cur = (
            self._col.find({"$or": due, vindex.PATH_DELETED: {"$ne": True}})
            .sort([(vindex.PATH_RECORDING_AT, 1), ("_id", 1)])
            .limit(limit)
        )
        out = list(cur)
        for d in out:
            enr = d.get("enrichment")
            if not isinstance(enr, dict):
                enr = d["enrichment"] = {}
            enr["generation"] = int(enr.get("generation") or 0)
        return out
