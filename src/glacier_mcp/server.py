"""MCP server (streamable-http) exposing the polygon tool surface.

Runs on port 8766 by default. Shares a single :class:`PolygonState`
instance with the FastAPI map server (``app.py``) so agent edits and
mouse edits operate on the same geometry.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .state import PolygonState

# Single shared instance for the process.
_state = PolygonState()

# ``stateless_http=True`` lets clients connect/reconnect without sticky
# sessions, which is what the OpenLayers UI + agent pair expects.
mcp = FastMCP(
    "glacier-mcp",
    stateless_http=True,
    json_response=True,
)


def get_state() -> PolygonState:
    """Return the shared PolygonState. Used by app.py and tests."""
    return _state


# ---- tool registration ----------------------------------------------------
# Tools are thin wrappers over PolygonState; validation lives in state.py.


@mcp.tool()
def get_polygon() -> dict:
    """Return the current polygon as a GeoJSON Feature (no CRS embedded)."""
    return _state.to_geojson()


@mcp.tool()
def get_vertices() -> list[dict]:
    """Return the exterior ring as a list of ``{index, x, y}``."""
    return [
        {"index": i, "x": x, "y": y}
        for i, (x, y) in enumerate(_state.get_vertices())
    ]


@mcp.tool()
def move_vertex(index: int, dx: float, dy: float) -> dict:
    """Move vertex ``index`` by (dx, dy) in CRS units."""
    _state.move_vertex(index, dx, dy)
    return {"ok": True, "vertices": get_vertices()}


@mcp.tool()
def add_vertex(index: int, x: float, y: float) -> dict:
    """Insert a vertex *before* position ``index`` (append if index == n)."""
    _state.add_vertex(index, (x, y))
    return {"ok": True, "vertices": get_vertices()}


@mcp.tool()
def delete_vertex(index: int) -> dict:
    """Remove vertex at ``index``. Refuses if fewer than 3 would remain."""
    _state.delete_vertex(index)
    return {"ok": True, "vertices": get_vertices()}


@mcp.tool()
def translate_polygon(dx: float, dy: float) -> dict:
    """Move the whole polygon by (dx, dy) in CRS units."""
    _state.translate(dx, dy)
    return {"ok": True, "polygon": _state.to_geojson()}


@mcp.tool()
def smooth_polygon(tolerance: float = 0.0) -> dict:
    """Smooth the boundary. ``tolerance`` is reserved (Chaikin only)."""
    _state.smooth(tolerance)
    return {"ok": True, "vertices": get_vertices()}


@mcp.tool()
def undo() -> dict:
    """Undo the last edit. Fails if there is nothing to undo."""
    _state.undo()
    return {"ok": True, "polygon": _state.to_geojson()}


@mcp.tool()
def redo() -> dict:
    """Redo the last undone edit. Fails if there is nothing to redo."""
    _state.redo()
    return {"ok": True, "polygon": _state.to_geojson()}


def run(host: str = "127.0.0.1", port: int = 8766) -> None:
    """Start the streamable-http MCP server on ``host:port``."""
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.run(transport="streamable-http")
