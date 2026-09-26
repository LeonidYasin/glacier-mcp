"""Entry point: ``python -m glacier_mcp``.

Starts two servers in one process:
  * HTTP + WebSocket UI on http://localhost:8765 (OpenLayers map)
  * MCP streamable-http endpoint on http://localhost:8766 (for the agent)

Both share the same PolygonState via ``server.get_state()``.
"""

from __future__ import annotations

import sys
import threading

from . import __version__
from .app import run as run_ui
from .server import run as run_mcp


def main() -> None:
    if "--version" in sys.argv:
        print(f"glacier-mcp {__version__}")
        return

    print(
        f"glacier-mcp {__version__}\n"
        "Map UI:  http://127.0.0.1:8765\n"
        "MCP:     http://127.0.0.1:8766\n"
    )

    # MCP in a background thread so the UI event loop stays responsive.
    mcp_thread = threading.Thread(target=run_mcp, name="mcp", daemon=True)
    mcp_thread.start()

    run_ui()


if __name__ == "__main__":
    main()
