"""Sync-state store: watermark, webhook dedupe keys, last run, recent errors, counters.

:class:`MongoStateStore` persists everything so it survives a restart:

* ``sync_state`` (:data:`STATE_COLLECTION`): a single ``{_id: "state"}`` document holding
  ``watermark``, ``last_run {started, finished, stats}``, ``errors`` (bounded, newest last),
  ``counters {name: int}`` and ``last_webhook_at``;
* ``webhook_dedupe`` (:data:`DEDUPE_COLLECTION`): one ``{_id: key, seen_at}`` document per
  dedupe key. ``_id`` is Mongo's built-in unique index, so :meth:`mark_seen` is an atomic
  insert that reports a duplicate via DuplicateKeyError -> ``False``.

:class:`MemoryStateStore` is the in-memory fake with identical semantics, for tests.
No third-party import happens here; callers inject the database (see
:func:`pocketsync.sinks.mongo.connect`).
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from pocketsync.sinks.mongo import is_duplicate_key

STATE_COLLECTION = "sync_state"
DEDUPE_COLLECTION = "webhook_dedupe"
STATE_ID = "state"
DEFAULT_MAX_ERRORS = 50
COUNTER_WEBHOOK_MISSED = "webhook_missed"
#: A webhook inbox entry is retried this many times, then left ``failed`` (errors on record).
INBOX_MAX_ATTEMPTS = 5
INBOX_PENDING, INBOX_DONE, INBOX_FAILED = "pending", "done", "failed"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@runtime_checkable
class StateStore(Protocol):
    """Persisted sync bookkeeping. Timestamps are ISO-8601 strings."""

    def get_watermark(self) -> str | None: ...

    def set_watermark(self, ts: str) -> None: ...

    def get_landing_schema(self) -> int | None:
        """The ``LANDING_SCHEMA`` of the pocketsync that last completed a pass (None: never)."""
        ...

    def set_landing_schema(self, version: int) -> None: ...

    def seen(self, dedupe_key: str) -> bool: ...

    def mark_seen(self, dedupe_key: str, item: dict[str, Any] | None = None) -> bool:
        """Atomically record ``dedupe_key``; True if new, False if already seen.

        With ``item`` the key doubles as a durable inbox entry: it stays ``pending`` (and is
        returned by :meth:`inbox_pending`) until :meth:`inbox_done`, so an acknowledged
        webhook survives a crash or a failed handler.
        """
        ...

    def inbox_pending(self) -> list[dict[str, Any]]:
        """Pending inbox entries, oldest first: ``{"key", "item", "attempts"}``."""
        ...

    def inbox_done(self, dedupe_key: str) -> None: ...

    def inbox_fail(self, dedupe_key: str, error: str) -> None:
        """Count a failed attempt; after ``INBOX_MAX_ATTEMPTS`` the entry is ``failed``."""
        ...

    def record_run(self, started: str, finished: str, stats: dict[str, Any]) -> None: ...

    def last_run(self) -> dict[str, Any] | None: ...

    def record_error(self, where: str, message: str, *, when: str | None = None) -> None:
        """Append an error; only the last ``max_errors`` are kept."""
        ...

    def errors(self) -> list[dict[str, Any]]: ...

    def increment(self, name: str, by: int = 1) -> int:
        """Increment counter ``name`` and return its new value."""
        ...

    def counters(self) -> dict[str, int]: ...

    def set_last_webhook_at(self, ts: str) -> None: ...

    def get_last_webhook_at(self) -> str | None: ...


class MemoryStateStore:
    """In-memory :class:`StateStore` (not persisted) for tests."""

    def __init__(self, *, max_errors: int = DEFAULT_MAX_ERRORS) -> None:
        self.max_errors = max_errors
        self._watermark: str | None = None
        self._seen: dict[str, str] = {}
        self._last_run: dict[str, Any] | None = None
        self._errors: list[dict[str, Any]] = []
        self._counters: dict[str, int] = {}
        self._last_webhook_at: str | None = None
        self._inbox: dict[str, dict[str, Any]] = {}  # insertion order = oldest first
        self._landing_schema: int | None = None

    def get_watermark(self) -> str | None:
        return self._watermark

    def set_watermark(self, ts: str) -> None:
        self._watermark = ts

    def get_landing_schema(self) -> int | None:
        return self._landing_schema

    def set_landing_schema(self, version: int) -> None:
        self._landing_schema = int(version)

    def seen(self, dedupe_key: str) -> bool:
        return dedupe_key in self._seen

    def mark_seen(self, dedupe_key: str, item: dict[str, Any] | None = None) -> bool:
        if dedupe_key in self._seen:
            return False
        self._seen[dedupe_key] = _now()
        if item is not None:
            self._inbox[dedupe_key] = {"item": copy.deepcopy(item), "attempts": 0}
        return True

    def inbox_pending(self) -> list[dict[str, Any]]:
        return [
            {"key": k, "item": copy.deepcopy(e["item"]), "attempts": e["attempts"]}
            for k, e in self._inbox.items()
            if e.get("status", INBOX_PENDING) == INBOX_PENDING
        ]

    def inbox_done(self, dedupe_key: str) -> None:
        self._inbox.pop(dedupe_key, None)

    def inbox_fail(self, dedupe_key: str, error: str) -> None:
        e = self._inbox.get(dedupe_key)
        if e is None:
            return
        e["attempts"] += 1
        e["error"] = error
        if e["attempts"] >= INBOX_MAX_ATTEMPTS:
            e["status"] = INBOX_FAILED

    def record_run(self, started: str, finished: str, stats: dict[str, Any]) -> None:
        self._last_run = {"started": started, "finished": finished, "stats": dict(stats)}

    def last_run(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._last_run)

    def record_error(self, where: str, message: str, *, when: str | None = None) -> None:
        self._errors.append({"where": where, "message": message, "at": when or _now()})
        del self._errors[: -self.max_errors]

    def errors(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._errors)

    def increment(self, name: str, by: int = 1) -> int:
        self._counters[name] = self._counters.get(name, 0) + by
        return self._counters[name]

    def counters(self) -> dict[str, int]:
        return dict(self._counters)

    def set_last_webhook_at(self, ts: str) -> None:
        self._last_webhook_at = ts

    def get_last_webhook_at(self) -> str | None:
        return self._last_webhook_at


class MongoStateStore:
    """:class:`StateStore` persisted in ``sync_state`` + ``webhook_dedupe``."""

    def __init__(self, database: Any, *, max_errors: int = DEFAULT_MAX_ERRORS) -> None:
        self.max_errors = max_errors
        self._state = database[STATE_COLLECTION]
        self._dedupe = database[DEDUPE_COLLECTION]

    def __repr__(self) -> str:
        return f"MongoStateStore(collection={STATE_COLLECTION!r})"

    def ensure_indexes(self) -> None:
        """Idempotent. Dedupe keys are ``_id`` (unique by construction); index seen_at."""
        self._dedupe.create_index("seen_at")

    def _doc(self) -> dict[str, Any]:
        return self._state.find_one({"_id": STATE_ID}) or {}

    def _set(self, fields: dict[str, Any]) -> None:
        self._state.update_one({"_id": STATE_ID}, {"$set": fields}, upsert=True)

    def get_watermark(self) -> str | None:
        return self._doc().get("watermark")

    def set_watermark(self, ts: str) -> None:
        self._set({"watermark": ts})

    def get_landing_schema(self) -> int | None:
        value = self._doc().get("landing_schema")
        return None if value is None else int(value)

    def set_landing_schema(self, version: int) -> None:
        self._set({"landing_schema": int(version)})

    def seen(self, dedupe_key: str) -> bool:
        return self._dedupe.find_one({"_id": dedupe_key}, {"_id": 1}) is not None

    def mark_seen(self, dedupe_key: str, item: dict[str, Any] | None = None) -> bool:
        entry: dict[str, Any] = {"_id": dedupe_key, "seen_at": _now()}
        if item is not None:
            entry.update(status=INBOX_PENDING, item=item, attempts=0)
        try:
            self._dedupe.insert_one(entry)
        except Exception as exc:  # pymongo stays an optional import
            if is_duplicate_key(exc):
                return False
            raise
        return True

    def inbox_pending(self) -> list[dict[str, Any]]:
        cur = self._dedupe.find({"status": INBOX_PENDING}).sort("seen_at", 1)
        return [{"key": d["_id"], "item": d["item"], "attempts": d.get("attempts", 0)} for d in cur]

    def inbox_done(self, dedupe_key: str) -> None:
        self._dedupe.update_one(
            {"_id": dedupe_key},
            {"$set": {"status": INBOX_DONE}, "$unset": {"item": ""}},
        )

    def inbox_fail(self, dedupe_key: str, error: str) -> None:
        doc = self._dedupe.find_one_and_update(
            {"_id": dedupe_key, "status": INBOX_PENDING},
            {"$inc": {"attempts": 1}, "$set": {"error": error}},
            return_document=True,
        )
        if doc is not None and doc.get("attempts", 0) >= INBOX_MAX_ATTEMPTS:
            self._dedupe.update_one({"_id": dedupe_key}, {"$set": {"status": INBOX_FAILED}})

    def record_run(self, started: str, finished: str, stats: dict[str, Any]) -> None:
        self._set({"last_run": {"started": started, "finished": finished, "stats": dict(stats)}})

    def last_run(self) -> dict[str, Any] | None:
        return self._doc().get("last_run")

    def record_error(self, where: str, message: str, *, when: str | None = None) -> None:
        entry = {"where": where, "message": message, "at": when or _now()}
        self._state.update_one(
            {"_id": STATE_ID},
            {"$push": {"errors": {"$each": [entry], "$slice": -self.max_errors}}},
            upsert=True,
        )

    def errors(self) -> list[dict[str, Any]]:
        return list(self._doc().get("errors") or [])

    def increment(self, name: str, by: int = 1) -> int:
        # return_document=True is pymongo's ReturnDocument.AFTER (atomic read-your-increment).
        doc = self._state.find_one_and_update(
            {"_id": STATE_ID}, {"$inc": {f"counters.{name}": by}}, upsert=True, return_document=True
        )
        return int((doc.get("counters") or {}).get(name, 0))

    def counters(self) -> dict[str, int]:
        return dict(self._doc().get("counters") or {})

    def set_last_webhook_at(self, ts: str) -> None:
        self._set({"last_webhook_at": ts})

    def get_last_webhook_at(self) -> str | None:
        return self._doc().get("last_webhook_at")
