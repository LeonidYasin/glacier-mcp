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


# ---- Google Earth Engine ---------------------------------------------------
#
# These tools talk to Earth Engine on behalf of whatever user last completed
# the OAuth flow in the browser (see gee.set_active_user, called from
# app.py's /api/gee/auth/callback). The MCP transport has no HTTP session,
# so we deliberately use a single global active user — right trade-off for
# a single-user localhost app.


@mcp.tool()
def gee_status() -> dict:
    """Report whether anyone is signed in to Earth Engine.

    Useful for the agent to check before calling the heavier tools, and
    for debugging the OAuth flow.
    """
    from . import gee

    user = gee.get_active_user()
    return {
        "logged_in": user is not None,
        "email": user.email if user else None,
    }


@mcp.tool()
def gee_search_sentinel2(
    bbox: list[float],
    date_from: str,
    date_to: str,
    cloud_max: float = 20.0,
    limit: int = 20,
) -> list[dict]:
    """Search Sentinel-2 (Level-2A) scenes intersecting a bounding box.

    bbox: [west, south, east, north] in EPSG:4326 (lon/lat degrees).
    date_from, date_to: ISO dates, e.g. "2024-07-01".
    cloud_max: maximum scene cloud cover percentage (0-100).
    limit: cap on returned scenes.

    Returns a list of {id, date, cloud_cover, satellite, tile}. Requires
    that the user has signed in with Google in the browser first — call
    gee_status to check.
    """
    from . import gee

    try:
        gee._require_ee()
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    return gee.search_sentinel2(
        bbox=bbox,
        date_from=date_from,
        date_to=date_to,
        cloud_max=cloud_max,
        limit=limit,
    )


@mcp.tool()
def gee_extract_glaciers(
    bbox: list[float],
    scene_id: str,
    ndsi_threshold: float = 0.4,
    min_area_px: int = 50,
) -> list[list[list[float]]]:
    """Extract glacier outlines from a Sentinel-2 scene via NDSI.

    bbox: [west, south, east, north] in EPSG:4326.
    scene_id: a scene id from gee_search_sentinel2.
    ndsi_threshold: NDSI cutoff (0.4 conservative, 0.5 stricter).
    min_area_px: drop specks smaller than this many 10 m pixels.

    Returns a list of rings; each ring is a list of [lon, lat] vertices.
    This tool only *returns* the geometry — call gee_add_glaciers_to_map
    to actually draw them in the editor.
    """
    from . import gee

    try:
        gee._require_ee()
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    return gee.extract_glacier_contours(
        bbox=bbox,
        scene_id=scene_id,
        ndsi_threshold=ndsi_threshold,
        min_area_px=min_area_px,
    )


@mcp.tool()
def gee_add_glaciers_to_map(
    bbox: list[float],
    scene_id: str,
    ndsi_threshold: float = 0.4,
    min_area_px: int = 50,
    name_prefix: str = "gee_glacier",
) -> dict:
    """One-shot: detect glaciers and add them to the shared PolygonState.

    Equivalent to gee_extract_glaciers + add_polygon per ring, but done in
    a single MCP call. After this returns, the new outlines are visible in
    the map UI and in list_polygons.

    Returns {ok, added, start_index, total}.
    """
    from shapely.geometry import Polygon as _ShapelyPolygon

    from . import gee

    try:
        gee._require_ee()
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc

    rings = gee.extract_glacier_contours(
        bbox=bbox,
        scene_id=scene_id,
        ndsi_threshold=ndsi_threshold,
        min_area_px=min_area_px,
    )
    if not rings:
        return {
            "ok": True,
            "added": 0,
            "start_index": len(_state),
            "total": len(_state),
        }

    start_index = len(_state)
    added = 0
    for i, ring in enumerate(rings):
        try:
            poly = _ShapelyPolygon(ring)
        except Exception:  # noqa: BLE001 — bad ring, skip
            continue
        if not poly.is_valid or poly.area <= 0:
            continue
        _state.add_polygon(poly, name=f"{name_prefix}_{i + 1}")
        added += 1

    return {
        "ok": True,
        "added": added,
        "start_index": start_index,
        "total": len(_state),
    }


@mcp.tool()
def gee_list_basemap_presets() -> list[dict]:
    """List the named Sentinel-2 basemap visualisation presets.

    Returns a list of {name, label, bands|index, min, max, gamma, palette}
    entries. Use one of the ``name`` values as the ``preset`` argument of
    ``gee_get_basemap``, or copy the full entry into ``vis_params`` and
    tweak it. This is the catalogue the UI builds its basemap dropdown
    from, exposed here so the agent can pick a layer without hard-coding
    band names.
    """
    from . import gee

    return gee.basemap_presets()


@mcp.tool()
def gee_get_basemap(
    scene_id: str,
    preset: str = "true_color",
    vis_params: dict | None = None,
    bbox: list[float] | None = None,
    clip: bool = False,
) -> dict:
    """Return an XYZ tile URL for one Sentinel-2 scene (map basemap).

    Use this to draw a real satellite image *under* the glacier polygons
    instead of an empty background. The returned ``tile_url`` is a plain
    ``https://earthengine.googleapis.com/.../{z}/{x}/{y}`` template that
    OpenLayers / Leaflet can use directly — it is what the UI passes to
    ``ol.source.XYZ``.

    scene_id: a scene id from gee_search_sentinel2 (full id or bare
              suffix both work — the collection prefix is normalised).
    preset:   one of gee_list_basemap_presets() — ``true_color``,
              ``false_color_nir``, ``false_color_swir``, ``ndsi``,
              ``ndwi``, ``ndvi``, ``nir_gray``. Ignored when vis_params
              is given.
    vis_params: raw override — {bands, min, max, gamma, palette, index}.
              Use this for a custom band combination the presets do not
              cover (e.g. B5/B4/B3 red-edge false colour).
    bbox:     optional [west, south, east, north] in EPSG:4326. When
              given, it is also forwarded to the browser so the map can
              recenter on the scene.
    clip:     when True and bbox is given, clip the layer to the bbox.
    push:     when True (default) the resolved layer is also pushed to
              every open map tab over the WebSocket, so the image
              appears without the user clicking anything.

    Returns {map_id, token, tile_url, preset, scene_id, band_names,
    index, expires_in, pushed_to}. The map id lives ~24 h; call again to
    refresh.
    """
    from . import gee

    try:
        gee._require_ee()
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc

    result = gee.get_basemap(
        scene_id=scene_id,
        preset=preset,
        vis_params=vis_params,
        bbox=bbox,
        clip=clip,
    )

    # Hand the layer to the browser so it actually shows up on the map.
    # Lazy import: app.py pulls in titiler/rasterio, which we do not want
    # to import when the MCP server runs in a slim context.
    result["pushed_to"] = 0
    if push:
        try:
            import asyncio as _asyncio

            from .app import hub

            payload = {
                "tile_url": result["tile_url"],
                "scene_id": result["scene_id"],
                "preset": result["preset"],
                "bbox": bbox,
            }
            # FastMCP runs sync tools in a worker thread, so there is no
            # running loop here. Schedule the broadcast on the main loop
            # that owns the WebSocket hub.
            loop = getattr(hub, "loop", None)
            if loop is not None and loop.is_running():
                _asyncio.run_coroutine_threadsafe(hub.push_gee_basemap(payload), loop)
                result["pushed_to"] = "scheduled"
        except Exception as exc:  # noqa: BLE001 — push is best-effort
            result["push_error"] = str(exc)

    return result


def run(host: str = "127.0.0.1", port: int = 8766) -> None:
    """Start the streamable-http MCP server on ``host:port``."""
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.run(transport="streamable-http")
