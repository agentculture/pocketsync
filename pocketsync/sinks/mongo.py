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
                taxonomy_version, summary_vector, chunks, enriched_at, error, attempts}

``updated_at`` is pocketsync's write time and moves only when content changes.
``Recording.raw`` and ``fetched_at`` are *not* stored (bulky, volatile wire data).

Field groups: a content write ``$set``s only the content group plus ``content_hash`` /
``updated_at`` / ``files`` and resets enrichment to ``pending`` while ``$unset``-ing every
derived enrichment field (vectors, labels, model, ...) so stale vectors never describe new
content. :meth:`MongoSink.set_enrichment` writes only ``enrichment.*`` and only while the
stored ``content_hash`` still equals the hash the enrichment was computed from (CAS).
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
    }
)
# Derived from content; dropped whenever content changes.
_DERIVED = ENRICHMENT_FIELDS - {"status", "attempts"}

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
                "enrichment.attempts": 0,
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

    def set_enrichment(self, recording_id: str, expected_hash: str, payload: dict) -> bool:
        """CAS-write ``payload`` into ``enrichment.*``; False if content moved on (or no doc).

        Only keys in :data:`ENRICHMENT_FIELDS` are allowed (ValueError otherwise).
        """
        bad = set(payload) - ENRICHMENT_FIELDS
        if bad:
            raise ValueError(f"not enrichment fields: {sorted(bad)}")
        if not payload:
            return False
        update: dict[str, Any] = {"$set": {f"enrichment.{k}": v for k, v in payload.items()}}
        if "next_attempt_at" not in payload:
            update[_UNSET] = {PATH_NEXT_ATTEMPT: ""}  # a success clears any retry schedule
        res = self._col.update_one({"_id": recording_id, "content_hash": expected_hash}, update)
        return res.matched_count == 1

    def set_enrichment_error(
        self,
        recording_id: str,
        expected_hash: str,
        message: str,
        next_attempt_at: float | None = None,
    ) -> bool:
        """CAS-record an enrichment failure: status=error, error=message, attempts += 1.

        ``next_attempt_at`` (epoch seconds) schedules the automatic retry; None leaves it
        terminal (the attempt cap was reached).
        """
        fields: dict[str, Any] = {
            PATH_ENRICHMENT_STATUS: ENRICHMENT_ERROR,
            "enrichment.error": message,
        }
        update: dict[str, Any] = {"$set": fields, "$inc": {PATH_ENRICHMENT_ATTEMPTS: 1}}
        if next_attempt_at is None:
            update[_UNSET] = {PATH_NEXT_ATTEMPT: ""}
        else:
            fields[PATH_NEXT_ATTEMPT] = float(next_attempt_at)
        res = self._col.update_one(
            {"_id": recording_id, "content_hash": expected_hash},
            update,
        )
        return res.matched_count == 1

    def pending_enrichment(self, limit: int, now: float | None = None) -> list[dict[str, Any]]:
        """Docs awaiting enrichment (not deleted in Pocket), oldest first.

        Always the ``pending`` ones. With ``now`` (epoch seconds), also the ``error`` docs
        whose automatic retry is due and whose attempts are below the cap.
        """
        if limit <= 0:
            return []
        due: list[dict[str, Any]] = [{PATH_ENRICHMENT_STATUS: ENRICHMENT_PENDING}]
        if now is not None:
            due.append(
                {
                    PATH_ENRICHMENT_STATUS: ENRICHMENT_ERROR,
                    PATH_ENRICHMENT_ATTEMPTS: {"$lt": MAX_ENRICHMENT_ATTEMPTS},
                    PATH_NEXT_ATTEMPT: {"$lte": float(now)},
                }
            )
        cur = (
            self._col.find({"$or": due, vindex.PATH_DELETED: {"$ne": True}})
            .sort([(vindex.PATH_RECORDING_AT, 1), ("_id", 1)])
            .limit(limit)
        )
        return list(cur)
