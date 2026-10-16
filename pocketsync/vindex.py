"""Vector search index manager with a model/dimension guard.

Never mix vector spaces: every vector in the store, and the index over it, must
come from one embedding model at one dimensionality. :func:`check` enforces that and
points at ``pocketsync reembed --apply`` when it is violated.

Document shape (one doc per recording in :data:`COLLECTION`; owned by the store task)::

    recording_at, deleted_in_pocket
    enrichment.model, enrichment.dims, enrichment.labels,
    enrichment.summary_vector, enrichment.chunks[{index,start,end,text,vector}]

Two named indexes are created (see the ``*_INDEX`` constants). MongoDB's
vector-search-type docs describe a ``nestedRoot`` option for vectors nested in an
array of documents, yet another passage says arrays of documents cannot be indexed.
Because that is contradictory, the chunk index (``nestedRoot``) and the summary index
are separate. Verified against atlas-local 8.0.34 (tests/integration/test_atlas_local.py):
the ``nestedRoot`` chunk index builds and is queryable, and root-level filter fields are
accepted — but queries must put root-level predicates (e.g. ``enrichment.labels``) in
``$vectorSearch``'s ``parentFilter``; the same predicate under ``filter`` silently
returns no results.

Model/dims record: the index definition carries ``numDimensions``; the model name is
kept in a ``{_id: "vindex", model, dims}`` doc in :data:`META_COLLECTION` (same
database), because Atlas index definitions cannot hold arbitrary metadata.

No third-party import happens here: indexes are passed to pymongo as plain dicts,
so the module imports with pymongo absent. Callers supply the pymongo collection.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from pocketsync.cli._errors import EXIT_USER_ERROR, CliError

COLLECTION = "recordings"
META_COLLECTION = "pocketsync_meta"
META_ID = "vindex"

PATH_RECORDING_AT = "recording_at"
PATH_DELETED = "deleted_in_pocket"
PATH_MODEL = "enrichment.model"
PATH_DIMS = "enrichment.dims"
PATH_LABELS = "enrichment.labels"
PATH_SENSITIVE = "enrichment.filters.sensitive"
PATH_HAS_ACTION_ITEMS = "enrichment.filters.has_action_items"
PATH_SUMMARY_VECTOR = "enrichment.summary_vector"
PATH_CHUNKS = "enrichment.chunks"
PATH_CHUNK_VECTOR = "enrichment.chunks.vector"

SUMMARY_INDEX = "recordings_summary_vec"
CHUNK_INDEX = "recordings_chunk_vec"
RECREATE_WAIT_STEPS = 60
SIMILARITY = "cosine"
FILTER_PATHS = (
    PATH_LABELS,
    PATH_DELETED,
    PATH_RECORDING_AT,
    PATH_SENSITIVE,
    PATH_HAS_ACTION_ITEMS,
)

REMEDIATION = "run `pocketsync reembed --apply` to re-embed everything with the configured model"


def _vector_field(path: str, dims: int) -> dict[str, Any]:
    return {"type": "vector", "path": path, "numDimensions": dims, "similarity": SIMILARITY}


def _filters() -> list[dict[str, Any]]:
    return [{"type": "filter", "path": p} for p in FILTER_PATHS]


def summary_definition(dims: int) -> dict[str, Any]:
    return {"fields": [_vector_field(PATH_SUMMARY_VECTOR, dims), *_filters()]}


def chunk_definition(dims: int) -> dict[str, Any]:
    return {
        "nestedRoot": PATH_CHUNKS,
        "fields": [_vector_field(PATH_CHUNK_VECTOR, dims), *_filters()],
    }


def index_models(dims: int) -> list[dict[str, Any]]:
    """The index specs (pymongo accepts dicts) for the given dimensionality."""
    return [
        {"name": SUMMARY_INDEX, "type": "vectorSearch", "definition": summary_definition(dims)},
        {"name": CHUNK_INDEX, "type": "vectorSearch", "definition": chunk_definition(dims)},
    ]


def _existing(collection: Any) -> dict[str, dict[str, Any]]:
    return {i["name"]: i for i in collection.list_search_indexes()}


def _index_dims(index: dict[str, Any]) -> set[int]:
    definition = index.get("latestDefinition") or index.get("definition") or {}
    return {
        f["numDimensions"]
        for f in definition.get("fields", [])
        if f.get("type") == "vector" and "numDimensions" in f
    }


def _index_filters(index: dict[str, Any]) -> set[str]:
    definition = index.get("latestDefinition") or index.get("definition") or {}
    return {f["path"] for f in definition.get("fields", []) if f.get("type") == "filter"}


def _meta(collection: Any) -> dict[str, Any] | None:
    return collection.database[META_COLLECTION].find_one({"_id": META_ID})


def _mismatch(model: str, dims: int) -> dict[str, Any]:
    return {
        "$or": [
            {PATH_MODEL: {"$exists": True, "$ne": model}},
            {PATH_DIMS: {"$exists": True, "$ne": dims}},
        ]
    }


def _fail(detail: str) -> CliError:
    return CliError(EXIT_USER_ERROR, f"vector space mismatch: {detail}", REMEDIATION)


def check(collection: Any, model: str, dims: int) -> None:
    """Raise CliError when the index or any stored doc uses another model/dimension."""
    meta = _meta(collection)
    if meta and (meta.get("model") != model or meta.get("dims") != dims):
        raise _fail(
            f"index was built for {meta.get('model')}/{meta.get('dims')}, "
            f"configured {model}/{dims}"
        )
    for name, index in _existing(collection).items():
        if name in (SUMMARY_INDEX, CHUNK_INDEX) and _index_dims(index) - {dims}:
            raise _fail(f"index {name} has numDimensions {sorted(_index_dims(index))}, not {dims}")
    bad = next(iter(collection.find(_mismatch(model, dims), {"_id": 1}).limit(1)), None)
    if bad is not None:
        raise _fail(f"stored document {bad['_id']} uses a different model or dimension")


def reembed_plan(collection: Any, model: str, dims: int) -> list[Any]:
    """Ids of recordings whose stored vectors don't match model/dims (dry-run, no writes)."""
    return [d["_id"] for d in collection.find(_mismatch(model, dims), {"_id": 1})]


def _recreate(collection: Any, spec: dict[str, Any], sleep: Callable[[float], None]) -> None:
    """Drop and re-create one index (atlas-local cannot update a vectorSearch index in place)."""
    collection.drop_search_index(spec["name"])
    for _ in range(RECREATE_WAIT_STEPS):
        if spec["name"] not in _existing(collection):
            break
        sleep(0.5)
    collection.create_search_index(spec)


def ensure_index(
    collection: Any, model: str, dims: int, *, sleep: Callable[[float], None] = time.sleep
) -> list[str]:
    """Idempotently create both vector indexes; return the names created this call.

    A present index whose filter fields differ from the wanted ones (order-insensitive)
    is updated in place via ``update_search_index``; those names are not in the result.
    atlas-local (mongot 1.77) rejects that for vectorSearch indexes (``"mappings" is
    required``), so on any update failure the index is dropped and re-created instead; vector
    search is unavailable for that index until it is queryable again (seconds on a small
    store). :func:`check` stays dims/model-only, so filter drift never blocks startup.
    """
    check(collection, model, dims)
    present = _existing(collection)
    created = []
    for spec in index_models(dims):
        if spec["name"] not in present:
            collection.create_search_index(spec)
            created.append(spec["name"])
        elif _index_filters(present[spec["name"]]) != set(FILTER_PATHS):
            try:
                collection.update_search_index(spec["name"], spec["definition"])
            except Exception:  # noqa: BLE001 - any refusal falls back to drop + re-create
                _recreate(collection, spec, sleep)
    collection.database[META_COLLECTION].replace_one(
        {"_id": META_ID}, {"_id": META_ID, "model": model, "dims": dims}, upsert=True
    )
    return created
