"""The one verb registry behind every "talk to recordings" surface: CLI, MCP and web.

Cited (cite-don't-import) from culture-rules ``culture_rules/cli/registry.py`` (itself from
agentfront), trimmed to pocketsync's needs and owned here. Each :class:`Verb` is registered
once in :data:`REGISTRY`. The CLI builds its commands from it
(:mod:`pocketsync.cli._commands.verbs`), the MCP server builds its tools from it, the web API
dispatches to it, and ``tests/test_surface_parity.py`` enumerates it, so no surface can grow a
verb, a parameter or a write path of its own.

A handler takes a :class:`Context` (the runtime plus ``apply``) and keyword parameters, calls
:mod:`pocketsync.recall`, and returns a plain JSON-able dict. Every surface calls it through
:meth:`Verb.invoke`, which validates the arguments against the verb's params schema, fills in
defaults and keeps ``apply`` off unless a mutating verb is explicitly asked to commit.

No third-party imports: importing this module (and so building any surface's verb list) never
pulls in pymongo, mcp or anything else outside the standard library.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pocketsync.cli._errors import EXIT_USER_ERROR, CliError
from pocketsync.recall import enrich as recall_enrich
from pocketsync.recall import read as recall_read
from pocketsync.recall import search as recall_search

__all__ = ["REGISTRY", "Context", "Param", "Verb", "get"]

_PY_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
}

APPLY_DESCRIPTION = "false = dry run (change nothing); true = commit the write"


def _bad(message: str, remediation: str) -> CliError:
    return CliError(EXIT_USER_ERROR, message, remediation)


@dataclass(frozen=True)
class Param:
    """One verb parameter. ``type`` is a JSON Schema scalar type.

    ``repeatable`` makes the value a list of that type (``--label a --label b`` on the CLI,
    a JSON array elsewhere). ``positional`` only affects the CLI spelling. A ``default`` of
    None means "absent"; the handler receives None.
    """

    name: str
    type: str = "string"
    help: str = ""
    required: bool = False
    repeatable: bool = False
    default: Any = None
    choices: tuple[Any, ...] | None = None
    positional: bool = False

    def __post_init__(self) -> None:
        if self.type not in _PY_TYPES:
            raise ValueError(f"unsupported param type {self.type!r}")

    def schema(self) -> dict[str, Any]:
        item: dict[str, Any] = {"type": self.type}
        if self.choices is not None:
            item["enum"] = list(self.choices)
        out: dict[str, Any] = {"type": "array", "items": item} if self.repeatable else dict(item)
        out["description"] = self.help
        if self.default is not None:
            out["default"] = self.default
        return out

    def _check_scalar(self, verb: str, value: Any) -> None:
        ok = isinstance(value, _PY_TYPES[self.type])
        if self.type != "boolean" and isinstance(value, bool):
            ok = False
        if not ok:
            raise _bad(
                f"{verb}: {self.name} must be {self.type}, not {type(value).__name__}",
                f"see `pocketsync explain {verb}` for the parameter types",
            )
        if self.choices is not None and value not in self.choices:
            raise _bad(
                f"{verb}: invalid {self.name} {value!r}",
                f"use one of: {', '.join(map(str, self.choices))}",
            )

    def check(self, verb: str, value: Any) -> Any:
        """Validate one supplied value (None = absent); return it unchanged."""
        if value is None:
            return None
        if self.repeatable:
            if not isinstance(value, list):
                raise _bad(
                    f"{verb}: {self.name} must be a list of {self.type}",
                    f"pass {self.name} as an array (CLI: repeat the flag)",
                )
            for v in value:
                self._check_scalar(verb, v)
            return list(value)
        self._check_scalar(verb, value)
        return value


@dataclass
class Context:
    """What a handler runs against: the CLI runtime and whether a write is committed."""

    runtime: Any
    apply: bool = False


Handler = Callable[..., dict[str, Any]]


@dataclass
class Verb:
    name: str
    summary: str
    handler: Handler
    params: tuple[Param, ...] = ()
    mutating: bool = False

    @property
    def tool_name(self) -> str:
        """Stable MCP tool name (the verb name; the MCP client namespaces by server)."""
        return self.name

    def params_schema(self) -> dict[str, Any]:
        """JSON Schema of the arguments; a mutating verb gains ``apply`` (default off)."""
        props = {p.name: p.schema() for p in self.params}
        if self.mutating:
            props["apply"] = {"type": "boolean", "default": False, "description": APPLY_DESCRIPTION}
        return {
            "type": "object",
            "properties": props,
            "required": [p.name for p in self.params if p.required],
            "additionalProperties": False,
        }

    def bind(self, arguments: Mapping[str, Any] | None) -> tuple[dict[str, Any], bool]:
        """Validate ``arguments`` against the schema; return ``(kwargs, apply)``.

        Unknown names, wrong types, values outside ``choices`` and missing required params
        raise :class:`CliError` (exit 1). ``apply`` is accepted only on a mutating verb and
        defaults to False.
        """
        args = dict(arguments or {})
        apply = args.pop("apply", None) if self.mutating else None
        if apply is not None and not isinstance(apply, bool):
            raise _bad(f"{self.name}: apply must be boolean", "pass apply: true or false")
        known = {p.name for p in self.params}
        unknown = sorted(set(args) - known)
        if unknown:
            raise _bad(
                f"{self.name}: unknown argument(s): {', '.join(unknown)}",
                f"valid arguments: {', '.join(sorted(self.params_schema()['properties']))}",
            )
        kwargs: dict[str, Any] = {}
        for p in self.params:
            value = p.check(self.name, args.get(p.name))
            if value is None:
                if p.required:
                    raise _bad(
                        f"{self.name}: missing required argument {p.name!r}",
                        f"see `pocketsync explain {self.name}`",
                    )
                value = p.default
            kwargs[p.name] = value
        return kwargs, bool(apply)

    def invoke(self, runtime: Any, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Validate ``arguments`` and run the handler: the one call every surface makes."""
        kwargs, apply = self.bind(arguments)
        return self.handler(Context(runtime, apply), **kwargs)


# -- handlers (they only call pocketsync.recall) ---------------------------------------------


def _store(ctx: Context) -> Any:
    ctx.runtime.settings.require("mongo_uri")
    return ctx.runtime.store_sink()


def _collection(ctx: Context) -> Any:
    return _store(ctx).collection


def _enrich(
    ctx: Context,
    *,
    all: bool,  # noqa: A002 - mirrors the --all flag
    since: str | None,
    until: str | None,
    id: list[str] | None,  # noqa: A002 - mirrors the --id flag
    label: str | None,
    status: str | None,
    only: str | None,
) -> dict[str, Any]:
    return recall_enrich.plan_enrich(
        _store(ctx),
        all=bool(all),
        since=since,
        until=until,
        ids=id,
        label=label,
        status=status,
        only=only,
        apply=ctx.apply,
    )


def _search(
    ctx: Context,
    *,
    query: str,
    label: list[str] | None,
    since: str | None,
    until: str | None,
    has_action_items: bool | None,
    exclude_sensitive: bool,
    limit: int,
) -> dict[str, Any]:
    col = _collection(ctx)
    return recall_search.search(
        col,
        ctx.runtime.gateway(),
        query,
        labels=label,
        since=since,
        until=until,
        has_action_items=has_action_items,
        exclude_sensitive=bool(exclude_sensitive),
        limit=limit,
        search_fn=ctx.runtime.search_fn(),
    )


def _get(ctx: Context, *, id: str) -> dict[str, Any]:  # noqa: A002
    return recall_read.get_recording(_collection(ctx), id)


def _transcript(
    ctx: Context, *, id: str, start: float | None, end: float | None  # noqa: A002
) -> dict[str, Any]:
    return recall_read.get_transcript(_collection(ctx), id, start=start, end=end)


def _recordings(
    ctx: Context,
    *,
    since: str | None,
    until: str | None,
    label: str | None,
    limit: int,
    exclude_sensitive: bool,
) -> dict[str, Any]:
    return recall_read.list_recordings(
        _collection(ctx),
        since=since,
        until=until,
        label=label,
        limit=limit,
        exclude_sensitive=bool(exclude_sensitive),
    )


def _actions(
    ctx: Context, *, open_only: bool, limit: int | None, exclude_sensitive: bool
) -> dict[str, Any]:
    return recall_read.list_action_items(
        _collection(ctx),
        open_only=bool(open_only),
        limit=limit,
        exclude_sensitive=bool(exclude_sensitive),
    )


# -- the registry ---------------------------------------------------------------------------

_SINCE = Param("since", help="Only recordings at or after this ISO date/datetime.")
_UNTIL = Param("until", help="Only recordings up to this ISO date/datetime (a date is inclusive).")
_EXCLUDE_SENSITIVE = Param(
    "exclude_sensitive",
    "boolean",
    "Drop recordings scored sensitive (absent scores count as not sensitive).",
    default=False,
)
_ID = Param("id", help="Recording id.", required=True, positional=True)

REGISTRY: tuple[Verb, ...] = (
    Verb(
        "enrich",
        "Select recordings and re-queue them for enrichment; dry run unless apply.",
        _enrich,
        (
            Param("all", "boolean", "Select every live recording.", default=False),
            Param("since", help="Recordings at or after this date (YYYY-MM-DD or ISO)."),
            Param("until", help="Recordings up to this date (a date is inclusive)."),
            Param("id", help="Select this recording id (repeatable).", repeatable=True),
            Param("label", help="Recordings carrying this label."),
            Param(
                "status",
                help="Recordings in this enrichment status.",
                choices=recall_enrich.STATUSES,
            ),
            Param(
                "only",
                help="Mode: all (full), classify (labels only) or embed (vectors only).",
                choices=("all", "classify", "embed"),
            ),
        ),
        mutating=True,
    ),
    Verb(
        "search",
        "Semantic search over recordings (keyword fallback when embedding is down).",
        _search,
        (
            Param("query", help="What to look for.", required=True, positional=True),
            Param("label", help="Only recordings with this label (repeatable).", repeatable=True),
            _SINCE,
            _UNTIL,
            Param(
                "has_action_items",
                "boolean",
                "true: only recordings with action items; false: only without; omit: either.",
            ),
            _EXCLUDE_SENSITIVE,
            Param(
                "limit",
                "integer",
                "Maximum results (1..1000).",
                default=recall_search.DEFAULT_LIMIT,
            ),
        ),
    ),
    Verb(
        "get",
        "One recording: metadata, summary, action items and labels (no vectors).",
        _get,
        (_ID,),
    ),
    Verb(
        "transcript",
        "A recording's speaker-attributed transcript, optionally a [start, end] window.",
        _transcript,
        (
            _ID,
            Param("start", "number", "Window start in seconds."),
            Param("end", "number", "Window end in seconds."),
        ),
    ),
    Verb(
        "recordings",
        "Stored recordings, newest first (not deleted, not merged).",
        _recordings,
        (
            _SINCE,
            _UNTIL,
            Param("label", help="Only recordings with this label."),
            Param(
                "limit",
                "integer",
                "Maximum recordings (1..1000).",
                default=recall_read.DEFAULT_LIST_LIMIT,
            ),
            _EXCLUDE_SENSITIVE,
        ),
    ),
    Verb(
        "actions",
        "Action items across stored recordings, newest recording first.",
        _actions,
        (
            Param("open_only", "boolean", "Only items not marked done.", default=True),
            Param("limit", "integer", "Maximum items (1..1000; omit for all)."),
            _EXCLUDE_SENSITIVE,
        ),
    ),
)


def get(name: str) -> Verb | None:
    """The registered verb called ``name``, or None."""
    return next((v for v in REGISTRY if v.name == name), None)
