"""Registry verbs as MCP tools, with no third-party imports.

Cited (cite-don't-import) from culture-rules ``culture_rules/mcp/tools.py``. One tool per
registry verb, named :attr:`Verb.tool_name` with :meth:`Verb.params_schema` as its input
schema. Every call goes through :meth:`Verb.invoke`, the same entry point the CLI uses, so
argument validation and the handler are shared. A mutating tool only commits when ``apply``
is exactly ``true``; any other value (or none) runs the dry run. ``apply`` on a read-only tool
is ignored.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pocketsync.cli._errors import CliError
from pocketsync.verbs import REGISTRY, Verb

__all__ = ["DRY_RUN_NOTE", "PRIVACY_NOTE", "ToolError", "call_tool", "tool_specs"]

DRY_RUN_NOTE = " Writes are a dry-run unless apply=true."
PRIVACY_NOTE = " Returns the operator's own recording content into the calling model's context."


class ToolError(Exception):
    """A tool call that failed; the message is shown to the calling model."""


def _by_tool_name() -> dict[str, Verb]:
    return {v.tool_name: v for v in REGISTRY}


def description(verb: Verb) -> str:
    """The tool description: the verb summary plus its write or privacy note."""
    return f"{verb.summary}{DRY_RUN_NOTE if verb.mutating else PRIVACY_NOTE}"


def tool_specs() -> list[dict[str, Any]]:
    """``{name, description, inputSchema}`` for every registry verb, in registry order."""
    return [
        {"name": v.tool_name, "description": description(v), "inputSchema": v.params_schema()}
        for v in REGISTRY
    ]


def _format(exc: CliError) -> str:
    return f"{exc.message} (hint: {exc.remediation})" if exc.remediation else exc.message


def call_tool(name: str, arguments: Mapping[str, Any] | None, runtime: Any) -> dict[str, Any]:
    """Run tool ``name`` against ``runtime`` and return the verb's JSON-able result.

    Raises :class:`ToolError` for an unknown tool, bad arguments (a :class:`CliError` from
    :meth:`Verb.invoke`, rendered with its hint) or any other failure in the handler.
    """
    verb = _by_tool_name().get(name)
    if verb is None:
        raise ToolError(
            f"unknown tool {name!r} (hint: available tools: {', '.join(_by_tool_name())})"
        )
    args = dict(arguments or {})
    apply = args.pop("apply", False) is True and verb.mutating
    if apply:
        args["apply"] = True
    try:
        return verb.invoke(runtime, args)
    except CliError as exc:
        raise ToolError(_format(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - every failure reaches the model as a tool error
        raise ToolError(f"{name}: {type(exc).__name__}: {exc}") from exc


def runtime_error(exc: BaseException) -> ToolError:
    """A :class:`ToolError` for a runtime that could not be built."""
    if isinstance(exc, CliError):
        return ToolError(_format(exc))
    return ToolError(f"runtime unavailable: {type(exc).__name__}: {exc}")
