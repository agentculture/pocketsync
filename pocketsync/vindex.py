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
are separate, and the chunk index definition is UNVERIFIED until an opt-in integration
test runs against a real atlas-local. Whether root-level filter fields are accepted
inside a ``nestedRoot`` index is part of that same uncertainty.

Model/dims record: the index definition carries ``numDimensions``; the model name is
kept in a ``{_id: "vindex", model, dims}`` doc in :data:`META_COLLECTION` (same
database), because Atlas index definitions cannot hold arbitrary metadata.

No third-party import happens here: indexes are passed to pymongo as plain dicts,
so the module imports with pymongo absent. Callers supply the pymongo collection.
"""

from __future__ import annotations

from typing import Any

from pocketsync.cli._errors import EXIT_USER_ERROR, CliError

COLLECTION = "recordings"
META_COLLECTION = "pocketsync_meta"
META_ID = "vindex"

PATH_RECORDING_AT = "recording_at"
PATH_DELETED = "deleted_in_pocket"
PATH_MODEL = "enrichment.model"
PATH_DIMS = "enrichment.dims"
PATH_LABELS = "enrichment.labels"
PATH_SUMMARY_VECTOR = "enrichment.summary_vector"
PATH_CHUNKS = "enrichment.chunks"
PATH_CHUNK_VECTOR = "enrichment.chunks.vector"

SUMMARY_INDEX = "recordings_summary_vec"
CHUNK_INDEX = "recordings_chunk_vec"
SIMILARITY = "cosine"
FILTER_PATHS = (PATH_LABELS, PATH_DELETED, PATH_RECORDING_AT)

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


def ensure_index(collection: Any, model: str, dims: int) -> list[str]:
    """Idempotently create both vector indexes; return the names created this call."""
    check(collection, model, dims)
    present = _existing(collection)
    created = []
    for spec in index_models(dims):
        if spec["name"] not in present:
            collection.create_search_index(spec)
            created.append(spec["name"])
    collection.database[META_COLLECTION].replace_one(
        {"_id": META_ID}, {"_id": META_ID, "model": model, "dims": dims}, upsert=True
    )
    return created
