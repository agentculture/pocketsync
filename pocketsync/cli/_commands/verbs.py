"""Registry-driven CLI commands: enrich, search, get, transcript, recordings, actions.

Each command is built from a :class:`pocketsync.verbs.Verb` in :data:`pocketsync.verbs.REGISTRY`
(no hand-written parser per verb): every :class:`~pocketsync.verbs.Param` becomes a flag
(``--since``, ``--exclude-sensitive`` / ``--no-exclude-sensitive``, a repeatable ``--label``)
or a positional, every command gets ``--json``, and a mutating verb gets ``--apply`` (off by
default, so it is a dry run). The handler is the same one the MCP server and the web API call,
through :meth:`~pocketsync.verbs.Verb.invoke`; ``--json`` prints its dict as-is and text mode
renders a few concise lines (vectors are never part of the dict, so never printed).
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

from pocketsync import verbs
from pocketsync.cli import _runtime
from pocketsync.cli._commands._common import add_apply, add_json, json_mode, kv_lines
from pocketsync.cli._errors import EXIT_USER_ERROR, CliError
from pocketsync.cli._output import emit_result

_ARG_TYPES: dict[str, Callable[[str], Any]] = {"string": str, "integer": int, "number": float}
_SNIPPET = 120


def _flag(name: str) -> str:
    return "--" + name.replace("_", "-")


def _add_param(p: argparse.ArgumentParser, param: verbs.Param) -> None:
    help_text = param.help
    if param.default is not None:
        help_text += f" (default: {param.default})"
    if param.positional:
        p.add_argument(param.name, type=_ARG_TYPES[param.type], help=help_text)
        return
    kw: dict[str, Any] = {"dest": param.name, "default": None, "help": help_text}
    if param.type == "boolean":
        kw["action"] = argparse.BooleanOptionalAction
    else:
        kw["type"] = _ARG_TYPES[param.type]
        if param.repeatable:
            kw["action"] = "append"
        if param.choices is not None:
            kw["choices"] = list(param.choices)
    p.add_argument(_flag(param.name), **kw)


def _arguments(verb: verbs.Verb, args: argparse.Namespace) -> dict[str, Any]:
    """Only the values the user gave; :meth:`Verb.invoke` fills in the defaults."""
    out = {
        p.name: getattr(args, p.name)
        for p in verb.params
        if getattr(args, p.name, None) is not None
    }
    if verb.mutating:
        out["apply"] = bool(args.apply)
    return out


def _make_cmd(name: str) -> Callable[[argparse.Namespace], None]:
    def cmd(args: argparse.Namespace) -> None:
        verb = verbs.get(name)
        if verb is None:  # only if the registry was changed after the parser was built
            raise CliError(EXIT_USER_ERROR, f"unknown verb {name!r}", "run 'pocketsync --help'")
        result = verb.invoke(_runtime.get(), _arguments(verb, args))
        if json_mode(args):
            emit_result(result, json_mode=True)
            return
        emit_result(_RENDER.get(name, _render_default)(result), json_mode=False)

    return cmd


def register(sub: argparse._SubParsersAction) -> None:
    for verb in verbs.REGISTRY:
        p = sub.add_parser(verb.name, help=verb.summary, description=verb.summary)
        for param in verb.params:
            _add_param(p, param)
        if verb.mutating:
            add_apply(p, f"the {verb.name}")
        add_json(p)
        p.set_defaults(func=_make_cmd(verb.name))


# -- text rendering -------------------------------------------------------------------------


def _clock(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)):
        return "-"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _snip(text: Any, width: int = _SNIPPET) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def _labels(row: dict[str, Any]) -> str:
    labels = row.get("labels") or []
    return f" [{', '.join(labels)}]" if labels else ""


def _render_default(result: dict[str, Any]) -> str:
    return kv_lines([(k, v) for k, v in result.items()]) if result else "(empty)"


def _render_enrich(r: dict[str, Any]) -> str:
    ids = r.get("ids") or []
    if not ids:
        return "nothing selected: no live recording matches those selectors"
    lines = [
        f"{r['count']} recording(s) selected for {r['mode']} re-enrichment "
        f"(~{r['estimated_gateway_calls']} gateway call(s)):"
    ]
    lines += [f"  {i}" for i in ids]
    if r.get("dry_run", True):
        lines.append("dry run (nothing written); pass --apply to re-queue them")
    else:
        lines.append(f"re-queued {r['updated']}; the enrichment worker will process them")
    return "\n".join(lines)


def _render_search(r: dict[str, Any]) -> str:
    head = f"{r['mode']} search for {r['query']!r}: {r['count']} result(s)"
    if r.get("reason"):
        head += f" ({r['reason']})"
    lines = [head]
    for hit in r.get("results") or []:
        lines.append(
            f"  {hit['id']}  {hit.get('recording_at') or '-'}  {hit.get('score', 0):.3f}  "
            f"{hit.get('title') or ''}{_labels(hit)}"
        )
        for c in hit.get("chunks") or []:
            lines.append(
                f"      [{_clock(c.get('start'))}-{_clock(c.get('end'))}] {_snip(c.get('text'))}"
            )
    return "\n".join(lines)


def _render_get(r: dict[str, Any]) -> str:
    text = kv_lines(
        [
            ("id", r["id"]),
            ("title", r.get("title")),
            ("recording_at", r.get("recording_at")),
            ("duration", _clock(r.get("duration"))),
            ("speakers", ", ".join(r.get("speakers") or []) or None),
            ("labels", ", ".join(r.get("labels") or []) or None),
            ("segments", r.get("segment_count")),
            ("enrichment", r.get("enrichment_status")),
        ]
    )
    if r.get("summary"):
        text += f"\n\nsummary: {_snip(r['summary'], 600)}"
    items = r.get("action_items") or []
    if items:
        text += "\n\naction items:"
        for a in items:
            text += f"\n  [{'x' if a.get('done') else ' '}] {a.get('text', '')}"
    return text


def _render_transcript(r: dict[str, Any]) -> str:
    lines = [f"{r['id']}  {r.get('title') or ''}  ({len(r.get('segments') or [])} segment(s))"]
    for s in r.get("segments") or []:
        who = s.get("speaker_name") or s.get("speaker") or "?"
        lines.append(f"[{_clock(s.get('start'))}] {who}: {s.get('text', '')}")
    return "\n".join(lines)


def _render_recordings(r: dict[str, Any]) -> str:
    rows = r.get("recordings") or []
    if not rows:
        return "no stored recordings match"
    return "\n".join(
        f"{row['id']}  {row.get('recording_at') or '-'}  {row.get('title') or ''}{_labels(row)}"
        for row in rows
    )


def _render_actions(r: dict[str, Any]) -> str:
    items = r.get("action_items") or []
    if not items:
        return "no action items"
    return "\n".join(
        f"[{'x' if a.get('done') else ' '}] {a.get('text', '')}  "
        f"({a['recording_id']}, {a.get('recording_at') or '-'})"
        for a in items
    )


_RENDER: dict[str, Callable[[dict[str, Any]], str]] = {
    "enrich": _render_enrich,
    "search": _render_search,
    "get": _render_get,
    "transcript": _render_transcript,
    "recordings": _render_recordings,
    "actions": _render_actions,
}
