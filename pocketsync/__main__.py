"""Entry point for ``python -m pocketsync``."""

from __future__ import annotations

import sys

from pocketsync.cli import main

if __name__ == "__main__":
    sys.exit(main())
