"""MCP server (streamable-http) exposing the polygon tool surface.

Runs on port 8766 by default. Shares a single :class:`PolygonState`
instance with the FastAPI map server (``app.py``) so agent edits and
mouse edits operate on the same geometry.

All edit tools take an optional ``polygon_index`` (default 0) so an agent
can target a specific glacier when several are digitised in one session.
Three new tools manage the collection itself: ``add_polygon``,
``remove_polygon``, ``list_polygons``.
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .state import PolygonState

# Single shared instance for the process.
_state = PolygonState()

# The MCP Python SDK turns DNS-rebinding protection on by default and checks
# BOTH the `Host` header and the `Origin` header of every request. Either
# one being outside the corresponding allow-list produces a 4xx (403 for a
# bad Origin, 421 Misdirected Request for a bad Host).
#
# Defaults only cover a localhost host WITHOUT our custom ports, and only
# localhost origins. That breaks two real clients of this server:
#   * the MCP Streamable HTTP client at http://127.0.0.1:8766/mcp sends
#     `Host: 127.0.0.1:8766`, which the default `allowed_hosts` rejects;
#   * the DeepSeek++ Chrome extension sends
#     `Origin: chrome-extension://<id>`, which the default `allowed_origins`
#     rejects.
# We keep the protection enabled and just widen both allow-lists to cover
# exactly what we need.
#
# We deliberately allow-list the SPECIFIC extension ID rather than
# `chrome-extension://*`: a wildcard would let any Chrome extension the user
# happens to install reach this server.
_MCP_ALLOWED_HOSTS = [
    "127.0.0.1",
    "127.0.0.1:*",
    "localhost",
    "localhost:*",
]

_MCP_ALLOWED_ORIGINS = [
    "http://127.0.0.1:*",
    "http://localhost:*",
    # DeepSeek++ Chrome extension (MCP Streamable HTTP client).
    "chrome-extension://kdmpkkahkhdmdhfkdihkopikgcocbpb",
]

# ``stateless_http=True`` lets clients connect/reconnect without sticky
# sessions, which is what the OpenLayers UI + agent pair expects.
mcp = FastMCP(
    "glacier-mcp",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        # NOTE: DNS-rebinding protection is DISABLED because the MCP SDK's
        # origin matching does NOT accept `chrome-extension://<id>` origins
        # (Chrome extensions have no host component, so the RFC-3986 origin
        # parser rejects them). Enabling the protection blocks the DeepSeek++
        # plugin with HTTP 403 `Invalid Origin header`, even with the
        # extension ID explicitly listed in `allowed_origins`.
        #
        # The server binds to 127.0.0.1 only, so remote DNS-rebinding is not
        # a threat in practice — a browser on another host cannot reach this
        # port, and a same-host browser cannot be used for DNS rebinding.
        enable_dns_rebinding_protection=False,
        allowed_hosts=_MCP_ALLOWED_HOSTS,
        allowed_origins=_MCP_ALLOWED_ORIGINS,
    ),
)


def get_state() -> PolygonState:
    """Return the shared PolygonState. Used by app.py and tests."""
    return _state


# ---- collection tools -----------------------------------------------------


@mcp.tool()
def list_polygons() -> list[dict]:
    """List every digitised glacier polygon with its index and name."""
    return [
        {"index": i, "name": name, "vertex_count": len(_state.get_vertices(i))}
        for i, name in enumerate(_state.names)
    ]


@mcp.tool()
def get_polygon_collection() -> dict:
    """Return all polygons as a GeoJSON FeatureCollection (no CRS embedded)."""
    return _state.to_geojson()


# ---- read -----------------------------------------------------------------


@mcp.tool()
def get_polygon(polygon_index: int = 0) -> dict:
    """Return one polygon as a GeoJSON Feature (no CRS embedded)."""
    collection = _state.to_geojson()
    features = collection["features"]
    if not features:
        raise ValueError(
            "No polygons in state yet. Draw one in the UI or use add_polygon()."
        )
    if not -len(features) <= polygon_index < len(features):
        raise ValueError(
            f"polygon_index {polygon_index} out of range (0..{len(features) - 1})"
        )
    return features[polygon_index]


@mcp.tool()
def get_vertices(polygon_index: int = 0) -> list[dict]:
    """Return the exterior ring as a list of ``{index, x, y}``."""
    return [
        {"index": i, "x": x, "y": y}
        for i, (x, y) in enumerate(_state.get_vertices(polygon_index))
    ]


# ---- edit -----------------------------------------------------------------


@mcp.tool()
def add_polygon(ring: list[list[float]], name: str | None = None) -> dict:
    """Append a new glacier polygon. ``ring`` is a list of [x, y] vertices.

    Returns ``{ok, index, total}`` where ``index`` is the position of the
    new polygon.
    """
    from shapely.geometry import Polygon as ShapelyPolygon

    polygon = ShapelyPolygon(ring)
    idx = _state.add_polygon(polygon, name=name)
    return {"ok": True, "index": idx, "total": len(_state)}


@mcp.tool()
def remove_polygon(polygon_index: int) -> dict:
    """Remove the polygon at ``polygon_index``.

    Emptying the collection is allowed — the UI handles the empty case and
    the user may want to start over.
    """
    _state.remove_polygon(polygon_index)
    return {"ok": True, "total": len(_state)}


@mcp.tool()
def move_vertex(
    vertex_index: int, dx: float, dy: float, polygon_index: int = 0
) -> dict:
    """Move vertex ``vertex_index`` of polygon ``polygon_index`` by (dx, dy)."""
    _state.move_vertex(polygon_index, vertex_index, dx, dy)
    return {"ok": True, "vertices": get_vertices(polygon_index)}


@mcp.tool()
def add_vertex(
    vertex_index: int, x: float, y: float, polygon_index: int = 0
) -> dict:
    """Insert a vertex before ``vertex_index`` (append if index == n)."""
    _state.add_vertex(polygon_index, vertex_index, (x, y))
    return {"ok": True, "vertices": get_vertices(polygon_index)}


@mcp.tool()
def delete_vertex(vertex_index: int, polygon_index: int = 0) -> dict:
    """Remove vertex at ``vertex_index``. Refuses if fewer than 3 remain."""
    _state.delete_vertex(polygon_index, vertex_index)
    return {"ok": True, "vertices": get_vertices(polygon_index)}


@mcp.tool()
def translate_polygon(dx: float, dy: float, polygon_index: int = 0) -> dict:
    """Move polygon ``polygon_index`` by (dx, dy) in CRS units."""
    _state.translate(polygon_index, dx, dy)
    return {"ok": True, "polygon": get_polygon(polygon_index)}


@mcp.tool()
def smooth_polygon(tolerance: float = 0.0, polygon_index: int = 0) -> dict:
    """Smooth the boundary of polygon ``polygon_index`` (Chaikin only)."""
    _state.smooth(polygon_index, tolerance)
    return {"ok": True, "vertices": get_vertices(polygon_index)}


# ---- visualisation --------------------------------------------------------


@mcp.tool()
async def capture_map(full_viewport: bool = False) -> dict:
    """Capture the current map view as a full-resolution PNG on disk.

    Requires a browser tab open at http://127.0.0.1:8765/ with the map UI
    loaded — the tool sends a WebSocket request to that tab, the browser
    renders the map with html2canvas, and the PNG comes back over the same
    socket.

    Arguments:
      full_viewport — if False, capture only #map (the map itself);
                      if True, capture the whole page (toolbar, HUD, panel).

    Returns a dict: {path, width, height, size_bytes}.

    The image itself is NOT returned through MCP: the transport has a
    ~64 KB response limit, and even a 512x288 PNG preview exceeds it. To
    actually see the picture, call ``get_capture_thumbnail(path)`` — it
    downscales on the server and returns a small JPEG that always fits.
    The full-resolution PNG is written to the ``captures/`` directory next
    to the frontend.
    """
    # Lazy import: app.py pulls in titiler/rasterio, which we do not want
    # to import when the MCP server is started in a slim context.
    from .app import hub, save_capture

    try:
        payload = await hub.request_capture(
            {"fullViewport": full_viewport},
            timeout=15.0,
        )
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    except TimeoutError as exc:
        raise ValueError(
            "Timed out waiting for the browser. "
            "Is the glacier-mcp UI tab still open and responsive?"
        ) from exc

    if not payload.get("ok"):
        raise ValueError(f"capture failed in browser: {payload.get('error')}")

    full_path = save_capture(payload["full_base64"])
    return {
        "path": str(full_path),
        "width": payload["width"],
        "height": payload["height"],
        "size_bytes": full_path.stat().st_size,
    }


@mcp.tool()
def get_capture_thumbnail(path: str, max_width: int = 256) -> list:
    """Return a small JPEG preview of a capture written by capture_map.

    The MCP transport refuses any response larger than ~64 KB, so the map
    PNG cannot be returned as-is. This tool downscales the saved PNG on
    the server (Pillow) and encodes it as JPEG quality=70, which lands
    around 5-15 KB for a typical map view — comfortably inside the limit.

    Arguments:
      path      — the ``path`` field returned by ``capture_map``.
      max_width — target width in pixels. 128/192/256 are safe; 512 may
                  exceed the 64 KB limit on detailed rasters.

    Returns the usual MCP content list: a small text block with the actual
    dimensions and encoded size, plus an image block (image/jpeg).
    """
    import base64 as _b64
    import io as _io
    from pathlib import Path as _Path

    from PIL import Image

    p = _Path(path)
    if not p.exists():
        raise ValueError(f"capture not found: {path}")

    img = Image.open(p)
    ratio = img.height / img.width if img.width else 1.0
    new_h = max(1, int(max_width * ratio))
    img = img.resize((max_width, new_h), Image.LANCZOS)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    buf = _io.BytesIO()
    img.save(buf, format="JPEG", quality=70, optimize=True)
    b64 = _b64.b64encode(buf.getvalue()).decode("ascii")

    return [
        {
            "type": "text",
            "text": json.dumps(
                {
                    "source_path": str(p),
                    "preview_width": max_width,
                    "preview_height": new_h,
                    "jpeg_base64_bytes": len(b64),
                }
            ),
        },
        {"type": "image", "data": b64, "mimeType": "image/jpeg"},
    ]


# ---- history --------------------------------------------------------------


@mcp.tool()
def undo() -> dict:
    """Undo the last edit (any polygon). Fails if history is empty."""
    _state.undo()
    return {"ok": True, "polygons": _state.to_geojson()}


@mcp.tool()
def redo() -> dict:
    """Redo the last undone edit. Fails if nothing to redo."""
    _state.redo()
    return {"ok": True, "polygons": _state.to_geojson()}


def run(host: str = "127.0.0.1", port: int = 8766) -> None:
    """Start the streamable-http MCP server on ``host:port``."""
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.run(transport="streamable-http")
