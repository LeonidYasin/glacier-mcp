"""Entry point: ``python -m glacier_mcp``.

Starts two servers in one process:

* HTTP + WebSocket UI on http://localhost:8765 (OpenLayers map)
* MCP streamable-http on http://localhost:8766 (for the agent)

Both share a single ``PolygonState`` instance. The MCP server runs in
a background thread (uvicorn owns the main thread for the map UI).
"""

from __future__ import annotations

import sys
import threading

import uvicorn

from . import __version__
from .server import run as run_mcp


class _McpThread(threading.Thread):
    def __init__(self, host: str, port: int) -> None:
        super().__init__(name="glacier-mcp-mcp", daemon=True)
        self.host = host
        self.port = port

    def run(self) -> None:  # pragma: no cover - thread entry
        try:
            run_mcp(host=self.host, port=self.port)
        except Exception as exc:  # noqa: BLE001 - surface, do not crash UI
            print(f"[mcp] failed to start: {exc}", file=sys.stderr)


def main() -> None:
    if "--version" in sys.argv:
        print(f"glacier-mcp {__version__}")
        return

    map_host, map_port = "127.0.0.1", 8765
    mcp_host, mcp_port = "127.0.0.1", 8766

    mcp_thread = _McpThread(mcp_host, mcp_port)
    mcp_thread.start()

    print(
        f"glacier-mcp {__version__}\n"
        f"Map UI: http://{map_host}:{map_port}\n"
        f"MCP   : http://{mcp_host}:{mcp_port}\n"
        "Ctrl+C to stop.\n"
    )
    uvicorn.run(
        "glacier_mcp.app:app",
        host=map_host,
        port=map_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
