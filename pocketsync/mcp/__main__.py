"""``python -m pocketsync.mcp`` — serve the verb registry as MCP tools over stdio.

stdout is reserved for the MCP protocol; the only diagnostic (the extra is missing) goes to
stderr.
"""

from __future__ import annotations

import sys

from pocketsync.mcp import server


def main() -> int:
    try:
        server.run_stdio()
    except server.ServerExtraMissing as exc:
        print(f"error: {exc}\nhint: {server.INSTALL_HINT}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
