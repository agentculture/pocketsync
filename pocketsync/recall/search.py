"""Semantic search over recordings: ``$vectorSearch`` on both indexes, keyword fallback.

:func:`search` embeds the query through the gateway with ``prompt_name="query"`` and
runs two aggregation pipelines through ``search_fn(collection, pipeline)`` (default
``collection.aggregate``; tests inject a fake because mongomock cannot run
``$vectorSearch``):

* the chunk index (:data:`~pocketsync.vindex.CHUNK_INDEX`, ``nestedRoot``) takes the
  recording-level predicates in ``parentFilter`` — under ``filter`` they silently match
  nothing (verified on atlas-local 8.0.34);
* the summary index (:data:`~pocketsync.vindex.SUMMARY_INDEX`) takes them in ``filter``.

Only predicates whose operators ``$vectorSearch`` filters support on that field type are
pushed into the search stage (``deleted_in_pocket`` equality, ``enrichment.labels``
``$in``, ``enrichment.filters.has_action_items`` ``$gte``). The rest — ``merged_into``
(not an index field), the ISO-string ``recording_at`` range and the opt-in
``exclude_sensitive`` (which must *keep* docs with no score) — go in a ``$match``
right after the search stage, which over-fetches to compensate. Every returned doc is
also re-checked in Python, so deleted/merged/filtered docs never appear whatever the
search backend returned.

Hits from both indexes are merged by recording (best score wins). Each result lists
its best-matching chunks, ranked by a local cosine between the query vector and the
stored chunk vectors, with ``start``/``end`` offsets. Vectors are never returned.

When embedding fails (:class:`~pocketsync.enrich.gateway.EnrichmentUnavailable` or any
other gateway :class:`CliError`), or the ``$vectorSearch`` stage itself fails (an index that
is missing, being rebuilt, or lacks a filter field because ``pocketsync web`` started before
``serve`` updated it), search degrades to a case-insensitive keyword match over title,
summary and chunk text with the same recording-level filters and reports
``mode: "keyword"`` plus the ``reason``. A search-stage failure is reported by exception
type only, so the reason never quotes a server message.
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable

from pocketsync import vindex
from pocketsync.cli._errors import EXIT_USER_ERROR, CliError
from pocketsync.recall.read import (
    DEFAULT_THRESHOLD,
    check_limit,
    collection_of,
    passes,
    recording_filter,
)

DEFAULT_LIMIT = 10
DEFAULT_CHUNKS_PER_RESULT = 3
OVERFETCH = 4
MIN_CANDIDATES = 100
MAX_CANDIDATES = 10000
KEYWORD_SCAN_LIMIT = 500
QUERY_PROMPT = "query"  # the gateway accepts query/document (Query is a 400; verified live)
MODE_VECTOR = "vector"
MODE_KEYWORD = "keyword"

SearchFn = Callable[[Any, list[dict[str, Any]]], Any]

_PROJECTION = {
    "_id": 1,
    "title": 1,
    "recording_at": 1,
    vindex.PATH_DELETED: 1,
    "merged_into": 1,
    vindex.PATH_LABELS: 1,
    "enrichment.filters": 1,
    vindex.PATH_CHUNKS: 1,
}


def _default_search(collection: Any, pipeline: list[dict[str, Any]]) -> Any:
    return collection.aggregate(pipeline)


def vector_filter(
    labels: list[str] | None = None, has_action_items: bool | None = None, threshold: float = 0.5
) -> dict[str, Any]:
    """Recording-level predicates safe to push into ``$vectorSearch`` (filter/parentFilter)."""
    flt: dict[str, Any] = {vindex.PATH_DELETED: False}
    if labels:
        flt[vindex.PATH_LABELS] = {"$in": list(labels)}
    if has_action_items is True:
        flt[vindex.PATH_HAS_ACTION_ITEMS] = {"$gte": threshold}
    return flt


def build_pipeline(
    index: str,
    query_vector: list[float],
    vflt: dict[str, Any],
    post: dict[str, Any],
    limit: int,
) -> list[dict[str, Any]]:
    """The aggregation pipeline for one index (chunk -> parentFilter, summary -> filter)."""
    fetch = limit * OVERFETCH
    stage: dict[str, Any] = {
        "index": index,
        "path": (
            vindex.PATH_CHUNK_VECTOR if index == vindex.CHUNK_INDEX else vindex.PATH_SUMMARY_VECTOR
        ),
        "queryVector": list(query_vector),
        "numCandidates": min(max(fetch * 10, MIN_CANDIDATES), MAX_CANDIDATES),
        "limit": fetch,
    }
    stage["parentFilter" if index == vindex.CHUNK_INDEX else "filter"] = vflt
    return [
        {"$vectorSearch": stage},
        {"$match": post},
        {"$limit": limit},
        {"$project": {**_PROJECTION, "score": {"$meta": "vectorSearchScore"}}},
    ]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if not na or not nb:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def _chunk_out(chunk: dict[str, Any], score: float) -> dict[str, Any]:
    return {
        "index": chunk.get("index"),
        "start": chunk.get("start"),
        "end": chunk.get("end"),
        "text": chunk.get("text", ""),
        "score": round(score, 6),
    }


def _chunks(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return list((doc.get("enrichment") or {}).get("chunks") or [])


def _result(doc: dict[str, Any], score: float, chunks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": doc["_id"],
        "title": doc.get("title"),
        "recording_at": doc.get("recording_at"),
        "labels": list((doc.get("enrichment") or {}).get("labels") or []),
        "score": round(float(score), 6),
        "chunks": chunks,
    }


def _best_chunks(doc: dict[str, Any], qvec: list[float], n: int) -> list[dict[str, Any]]:
    scored = [(_cosine(qvec, c.get("vector") or []), c) for c in _chunks(doc) if c.get("vector")]
    scored.sort(key=lambda sc: sc[0], reverse=True)
    return [_chunk_out(c, s) for s, c in scored[:n]]


def _vector_search(
    col: Any,
    qvec: list[float],
    vflt: dict[str, Any],
    post: dict[str, Any],
    limit: int,
    chunks_per_result: int,
    search_fn: SearchFn,
) -> list[dict[str, Any]]:
    best: dict[Any, tuple[float, dict[str, Any]]] = {}
    for index in (vindex.CHUNK_INDEX, vindex.SUMMARY_INDEX):
        for doc in search_fn(col, build_pipeline(index, qvec, vflt, post, limit)):
            if not passes(doc, post):
                continue
            score = float(doc.get("score") or 0.0)
            prev = best.get(doc["_id"])
            if prev is None or score > prev[0]:
                best[doc["_id"]] = (score, doc)
    ranked = sorted(best.values(), key=lambda sd: sd[0], reverse=True)[:limit]
    return [_result(d, s, _best_chunks(d, qvec, chunks_per_result)) for s, d in ranked]


def _keyword_search(
    col: Any, query: str, post: dict[str, Any], limit: int, chunks_per_result: int
) -> list[dict[str, Any]]:
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    rx = {"$regex": re.escape(query), "$options": "i"}
    flt = {
        **post,
        "$or": [{"title": rx}, {"summary": rx}, {f"{vindex.PATH_CHUNKS}.text": rx}],
    }
    proj = {**_PROJECTION, "summary": 1}
    scored = []
    for doc in col.find(flt, proj).limit(KEYWORD_SCAN_LIMIT):
        if not passes(doc, post):
            continue
        hits = []
        for c in _chunks(doc):
            n = len(pattern.findall(str(c.get("text", ""))))
            if n:
                hits.append((n, c))
        hits.sort(key=lambda nc: nc[0], reverse=True)
        total = sum(n for n, _ in hits)
        total += len(pattern.findall(str(doc.get("title") or "")))
        total += len(pattern.findall(str(doc.get("summary") or "")))
        if not total:
            continue
        chunks = [_chunk_out(c, float(n)) for n, c in hits[:chunks_per_result]]
        scored.append((total, str(doc.get("recording_at") or ""), doc, chunks))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [_result(d, float(total), ch) for total, _, d, ch in scored[:limit]]


def _keyword_result(
    col: Any,
    text: str,
    post: dict[str, Any],
    limit: int,
    chunks_per_result: int,
    detail: str,
    stage: str,
) -> dict[str, Any]:
    results = _keyword_search(col, text, post, limit, chunks_per_result)
    what = "query embedding" if stage == "embed" else "vector search"
    return {
        "mode": MODE_KEYWORD,
        "reason": f"{what} failed: {detail}",
        "query": text,
        "count": len(results),
        "results": results,
    }


def search(
    store: Any,
    gateway: Any,
    query: str,
    labels: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    has_action_items: bool | None = None,
    exclude_sensitive: bool = False,
    limit: int = DEFAULT_LIMIT,
    search_fn: SearchFn | None = None,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    chunks_per_result: int = DEFAULT_CHUNKS_PER_RESULT,
) -> dict[str, Any]:
    """Search recordings for ``query``; returns ``{mode, query, count, results[, reason]}``.

    ``exclude_sensitive`` is opt-in (default: sensitive recordings are returned); when set it
    drops docs whose ``enrichment.filters.sensitive`` >= ``threshold`` and keeps docs
    without a score. ``has_action_items`` None means "either".
    """
    text = (query or "").strip()
    if not text:
        raise CliError(EXIT_USER_ERROR, "empty search query", "pass a non-empty query")
    check_limit(limit)
    col = collection_of(store)
    post = recording_filter(
        labels=labels,
        since=since,
        until=until,
        has_action_items=has_action_items,
        exclude_sensitive=exclude_sensitive,
        threshold=threshold,
    )
    try:
        qvec = gateway.embed([text], prompt_name=QUERY_PROMPT)[0]
    except CliError as exc:  # EnrichmentUnavailable is a CliError
        return _keyword_result(col, text, post, limit, chunks_per_result, exc.message, "embed")
    vflt = vector_filter(labels, has_action_items, threshold)
    try:
        results = _vector_search(
            col, qvec, vflt, post, limit, chunks_per_result, search_fn or _default_search
        )
    except Exception as exc:  # noqa: BLE001 - e.g. pymongo OperationFailure mid-index-rebuild
        return _keyword_result(
            col, text, post, limit, chunks_per_result, type(exc).__name__, "search"
        )
    return {"mode": MODE_VECTOR, "query": text, "count": len(results), "results": results}
