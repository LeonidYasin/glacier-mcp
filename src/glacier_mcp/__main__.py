"""Entry point: ``python -m glacier_mcp``.

Commit 3 wires only the MCP side (port 8766). The OpenLayers map UI
(port 8765) lands in commit 4; until then this entry point prints a
short note and runs the MCP server.
"""

from __future__ import annotations

import sys

from . import __version__
from .server import run as run_mcp


def main() -> None:
    if "--version" in sys.argv:
        print(f"glacier-mcp {__version__}")
        return

    print(
        f"glacier-mcp {__version__}\n"
        "MCP streamable-http: http://127.0.0.1:8766\n"
        "Map UI: not implemented yet (commit 4)\n"
    )
    run_mcp()


if __name__ == "__main__":
    main()
