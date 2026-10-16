"""Read-only operations over the ``recordings`` collection (plain dicts in and out).

Every function takes ``store``: a pymongo-compatible collection or a
:class:`~pocketsync.sinks.mongo.MongoSink`. Outputs are JSON-safe dicts built from an
explicit field whitelist, so vectors (``enrichment.summary_vector``,
``enrichment.chunks[].vector``) never leave this module.

Shared recording-level filtering lives here too (:func:`recording_filter` for Mongo,
:func:`passes` for already-fetched docs) so search and the list verbs agree:

* deleted (``deleted_in_pocket``) and merged (``merged_into`` set) recordings are excluded;
* ``exclude_sensitive`` is opt-in (user decision c31): it drops docs whose
  ``enrichment.filters.sensitive`` is at or above the threshold; an absent score is
  treated as not sensitive;
* ``since``/``until`` take an ISO date or datetime; a bare-date ``until`` includes that day.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from pocketsync import vindex
from pocketsync.cli._errors import EXIT_USER_ERROR, CliError
from pocketsync.sinks.mongo import MongoSink

DEFAULT_THRESHOLD = 0.5
DEFAULT_LIST_LIMIT = 50
MAX_LIMIT = 1000


def collection_of(store: Any) -> Any:
    """The recordings collection for ``store`` (a MongoSink or a collection)."""
    return store.collection if isinstance(store, MongoSink) else store


def check_limit(limit: int | None, *, allow_none: bool = False) -> int | None:
    if limit is None and allow_none:
        return None
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise CliError(EXIT_USER_ERROR, f"invalid limit: {limit!r}", f"use 1..{MAX_LIMIT}")
    return limit


def _bounds(since: str | None, until: str | None) -> dict[str, str]:
    """Mongo comparison operators on the ISO-string ``recording_at`` (lexicographic)."""
    out: dict[str, str] = {}
    for name, value in (("since", since), ("until", until)):
        if value is None:
            continue
        text = str(value).strip()
        try:
            if len(text) == 10:
                day = date.fromisoformat(text)
            else:
                datetime.fromisoformat(text)
                day = None
        except ValueError as exc:
            raise CliError(
                EXIT_USER_ERROR,
                f"invalid {name}: {value!r}",
                "use an ISO date (YYYY-MM-DD) or datetime (YYYY-MM-DDTHH:MM:SSZ)",
            ) from exc
        if name == "since":
            out["$gte"] = text
        elif day is not None:
            out["$lt"] = (day + timedelta(days=1)).isoformat()
        else:
            out["$lte"] = text
    return out


def recording_filter(
    *,
    labels: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    has_action_items: bool | None = None,
    exclude_sensitive: bool = False,
    threshold: float = DEFAULT_THRESHOLD,
) -> dict[str, Any]:
    """Standard MQL for the recording-level filters (usable in ``find`` and ``$match``)."""
    flt: dict[str, Any] = {vindex.PATH_DELETED: {"$ne": True}, "merged_into": None}
    if labels:
        flt[vindex.PATH_LABELS] = {"$in": list(labels)}
    rng = _bounds(since, until)
    if rng:
        flt[vindex.PATH_RECORDING_AT] = rng
    if has_action_items is True:
        flt[vindex.PATH_HAS_ACTION_ITEMS] = {"$gte": threshold}
    elif has_action_items is False:
        flt[vindex.PATH_HAS_ACTION_ITEMS] = {"$not": {"$gte": threshold}}
    if exclude_sensitive:
        flt[vindex.PATH_SENSITIVE] = {"$not": {"$gte": threshold}}
    return flt


def _filter_score(doc: dict[str, Any], name: str) -> float | None:
    value = ((doc.get("enrichment") or {}).get("filters") or {}).get(name)
    return float(value) if isinstance(value, (int, float)) else None


def _labels_passes(doc: dict[str, Any], flt: dict[str, Any]) -> bool:
    if vindex.PATH_LABELS not in flt:
        return True
    labels = (doc.get("enrichment") or {}).get("labels") or []
    return bool(set(labels) & set(flt[vindex.PATH_LABELS]["$in"]))


def _range_passes(doc: dict[str, Any], flt: dict[str, Any]) -> bool:
    rng = flt.get(vindex.PATH_RECORDING_AT)
    if not rng:
        return True
    at = doc.get("recording_at")
    if not isinstance(at, str):
        return False
    if "$gte" in rng and at < rng["$gte"]:
        return False
    if "$lt" in rng and at >= rng["$lt"]:
        return False
    return not ("$lte" in rng and at > rng["$lte"])


def _score_passes(score: float | None, cond: dict[str, Any]) -> bool:
    if "$gte" in cond and (score is None or score < cond["$gte"]):
        return False
    return not ("$not" in cond and score is not None and score >= cond["$not"]["$gte"])


def _filters_pass(doc: dict[str, Any], flt: dict[str, Any]) -> bool:
    for path, name in (
        (vindex.PATH_HAS_ACTION_ITEMS, "has_action_items"),
        (vindex.PATH_SENSITIVE, "sensitive"),
    ):
        cond = flt.get(path)
        if cond is not None and not _score_passes(_filter_score(doc, name), cond):
            return False
    return True


def passes(doc: dict[str, Any], flt: dict[str, Any]) -> bool:
    """Evaluate a :func:`recording_filter` result against one fetched doc (in Python)."""
    if doc.get(vindex.PATH_DELETED) is True or doc.get("merged_into") is not None:
        return False
    return _labels_passes(doc, flt) and _range_passes(doc, flt) and _filters_pass(doc, flt)


def _enrichment(doc: dict[str, Any]) -> dict[str, Any]:
    return doc.get("enrichment") or {}


def _find_one(store: Any, recording_id: str, projection: dict[str, Any] | None = None):
    doc = collection_of(store).find_one({"_id": recording_id}, projection)
    if doc is None:
        raise CliError(
            EXIT_USER_ERROR,
            f"unknown recording: {recording_id}",
            "list recordings with `pocketsync recordings` (or search) to find an id",
        )
    return doc


def get_recording(store: Any, recording_id: str) -> dict[str, Any]:
    """One recording's metadata, summary, action items and labels (no segments, no vectors)."""
    doc = _find_one(store, recording_id)
    enr = _enrichment(doc)
    return {
        "id": doc["_id"],
        "title": doc.get("title"),
        "description": doc.get("description"),
        "recording_at": doc.get("recording_at"),
        "duration": doc.get("duration"),
        "language": doc.get("language"),
        "speakers": list(doc.get("speakers") or []),
        "tags": list(doc.get("tags") or []),
        "summary": doc.get("summary"),
        "action_items": [_action(a) for a in doc.get("action_items") or []],
        "segment_count": len(doc.get("segments") or []),
        "labels": list(enr.get("labels") or []),
        "filters": dict(enr.get("filters") or {}),
        "enrichment_status": enr.get("status"),
        "files": doc.get("files") or {},
        "deleted_in_pocket": bool(doc.get(vindex.PATH_DELETED, False)),
        "merged_into": doc.get("merged_into"),
        "updated_at": doc.get("updated_at"),
    }


def _action(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "text": item.get("text", ""),
        "done": bool(item.get("done", False)),
        "owner": item.get("owner"),
    }


def _segment(seg: dict[str, Any]) -> dict[str, Any]:
    return {k: seg.get(k) for k in ("start", "end", "text", "speaker", "speaker_name")}


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def get_transcript(
    store: Any, recording_id: str, start: float | None = None, end: float | None = None
) -> dict[str, Any]:
    """Speaker-attributed segments, optionally only those overlapping ``[start, end]`` (s)."""
    if start is not None and end is not None and start > end:
        raise CliError(EXIT_USER_ERROR, "start is after end", "pass start <= end (seconds)")
    doc = _find_one(store, recording_id, {"title": 1, "recording_at": 1, "segments": 1})
    out = []
    for seg in doc.get("segments") or []:
        s, e = _num(seg.get("start")), _num(seg.get("end"))
        e = s if e is None else e
        if start is not None and e is not None and e < start:
            continue
        if end is not None and s is not None and s > end:
            continue
        out.append(_segment(seg))
    return {
        "id": doc["_id"],
        "title": doc.get("title"),
        "recording_at": doc.get("recording_at"),
        "start": start,
        "end": end,
        "segments": out,
    }


_LIST_PROJECTION = {
    "title": 1,
    "recording_at": 1,
    "duration": 1,
    "action_items": 1,
    "enrichment.labels": 1,
    "enrichment.status": 1,
}


def list_recordings(
    store: Any,
    since: str | None = None,
    until: str | None = None,
    label: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    exclude_sensitive: bool = False,
    threshold: float = DEFAULT_THRESHOLD,
) -> dict[str, Any]:
    """Live (not deleted, not merged) recordings, newest first."""
    check_limit(limit)
    flt = recording_filter(
        labels=[label] if label else None,
        since=since,
        until=until,
        exclude_sensitive=exclude_sensitive,
        threshold=threshold,
    )
    cursor = (
        collection_of(store)
        .find(flt, _LIST_PROJECTION)
        .sort([(vindex.PATH_RECORDING_AT, -1), ("_id", 1)])
        .limit(limit)
    )
    rows = [
        {
            "id": d["_id"],
            "title": d.get("title"),
            "recording_at": d.get("recording_at"),
            "duration": d.get("duration"),
            "labels": list(_enrichment(d).get("labels") or []),
            "action_items": len(d.get("action_items") or []),
            "enrichment_status": _enrichment(d).get("status"),
        }
        for d in cursor
    ]
    return {"count": len(rows), "recordings": rows}


def list_action_items(
    store: Any,
    open_only: bool = False,
    limit: int | None = None,
    exclude_sensitive: bool = False,
    threshold: float = DEFAULT_THRESHOLD,
) -> dict[str, Any]:
    """Action items across live recordings, newest recording first (``limit`` = items)."""
    check_limit(limit, allow_none=True)
    flt = recording_filter(exclude_sensitive=exclude_sensitive, threshold=threshold)
    flt["action_items.0"] = {"$exists": True}
    cursor = (
        collection_of(store)
        .find(flt, {"title": 1, "recording_at": 1, "action_items": 1})
        .sort([(vindex.PATH_RECORDING_AT, -1), ("_id", 1)])
    )
    items: list[dict[str, Any]] = []
    for d in cursor:
        for i, raw in enumerate(d.get("action_items") or []):
            item = _action(raw)
            if open_only and item["done"]:
                continue
            items.append(
                {
                    "recording_id": d["_id"],
                    "title": d.get("title"),
                    "recording_at": d.get("recording_at"),
                    "index": i,
                    **item,
                }
            )
    if limit is not None:
        items = items[:limit]
    return {"count": len(items), "action_items": items}
