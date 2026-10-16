"""The MCP server: a low-level ``mcp`` SDK server whose tools come from the verb registry.

Cited (cite-don't-import) from culture-rules ``culture_rules/mcp/server.py``. Needs
``pip install 'pocketsync[mcp]'``; the SDK is imported lazily inside the functions, so
``import pocketsync.mcp`` never pulls it in. The runtime (settings, the Mongo store, the
lobes gateway) comes from :func:`pocketsync.cli._runtime.get`, the same as the CLI, and is
built lazily on the first tool call, then reused. Nothing here prints: stdout carries only
the MCP protocol.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from pocketsync.mcp import tools

__all__ = ["INSTALL_HINT", "ServerExtraMissing", "build_server", "run_stdio"]

INSTALL_HINT = "pip install 'pocketsync[mcp]'"
_EXTRA_MESSAGE = f"the MCP server needs the 'mcp' extra: {INSTALL_HINT}"


class ServerExtraMissing(ImportError):
    """The ``mcp`` extra is not installed."""


def _sdk():
    try:
        import mcp.types as types  # noqa: PLC0415 - optional extra
        from mcp.server.lowlevel import Server  # noqa: PLC0415
    except ImportError as exc:
        raise ServerExtraMissing(_EXTRA_MESSAGE) from exc
    return Server, types


def _default_runtime() -> Any:
    from pocketsync.cli import _runtime  # noqa: PLC0415 - built only when a tool is called

    return _runtime.get()


def build_server(runtime_factory: Callable[[], Any] | None = None):
    """An MCP ``Server`` exposing every registry verb; ``runtime_factory`` yields the runtime."""
    server_cls, types = _sdk()
    factory = runtime_factory or _default_runtime
    cache: list[Any] = []

    def runtime() -> Any:
        if not cache:
            try:
                cache.append(factory())
            except Exception as exc:  # noqa: BLE001 - reported to the model, retried next call
                raise tools.runtime_error(exc) from exc
        return cache[0]

    server = server_cls("pocketsync")

    @server.list_tools()
    async def _list() -> list[Any]:
        return [types.Tool(**spec) for spec in tools.tool_specs()]

    @server.call_tool(validate_input=False)  # Verb.invoke validates, as for the CLI
    async def _call(name: str, arguments: dict[str, Any] | None) -> Any:
        try:
            result = tools.call_tool(name, arguments, runtime())
        except tools.ToolError as exc:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(exc))], isError=True
            )
        return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    return server


def run_stdio() -> None:
    """Serve over stdio until the client disconnects.

    Raises :class:`ServerExtraMissing` when the ``mcp`` extra (the SDK and its ``anyio``) is
    not installed, before anything is served.
    """
    try:
        import anyio  # noqa: PLC0415 - optional extra (an mcp dependency)

        from mcp.server.stdio import stdio_server  # noqa: PLC0415
    except ImportError as exc:
        raise ServerExtraMissing(_EXTRA_MESSAGE) from exc

    server = build_server()

    async def main() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(main)
