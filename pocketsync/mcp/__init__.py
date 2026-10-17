"""The MCP server over the one verb registry (needs the ``mcp`` extra for the server).

Cited (cite-don't-import) from culture-rules ``culture_rules/mcp``. :mod:`.tools` is
dependency-free (registry -> tool specs and dispatch through :meth:`Verb.invoke`);
:mod:`.server` imports the official ``mcp`` SDK lazily, so importing this package never pulls
it in.
"""
