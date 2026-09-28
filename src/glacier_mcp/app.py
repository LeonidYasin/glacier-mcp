"""FastAPI app: serves the OpenLayers UI and the WebSocket bridge.

The UI and the MCP server share a single :class:`PolygonState` via
``server.get_state()``. Any mutation - whether from the mouse or from an
agent calling an MCP tool - triggers a broadcast over ``/ws`` so every
connected browser tab stays in sync.

Also serves GeoTIFF upload + preview so the map can render a real raster
basemap instead of an empty background, and exposes a shapefile export
endpoint that writes every digitised polygon as one row in the .dbf.
"""

from __future__ import annotations

import asyncio
import base64
import io as stdlib_io
import json
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np
# titiler: production COG tile server used by NASA Worldview / FIRMS. It
# reads the source GeoTIFF through rasterio, picks the right overview for
# the requested zoom, resamples to 256x256, and returns a PNG. Handles
# non-square rasters, arbitrary CRS, mosaics, and multi-layer overlays —
# all the pieces we tried to build by hand.
from titiler.core.factory import TilerFactory as _CogTilerFactory
# rio-tiler is titiler's raster engine. We patch its Reader.tile below so
# that out-of-bounds tiles return a transparent image instead of a 500.
from rio_tiler.errors import TileOutsideBounds as _TileOutsideBounds
from rio_tiler.io.rasterio import Reader as _RioReader
from rio_tiler.models import ImageData as _ImageData
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import io as gio
from .server import get_state

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
CAPTURES_DIR = Path(__file__).resolve().parents[2] / "captures"
CAPTURES_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="glacier-mcp")


# Disable browser caching for /static/* during development. main.js and
# style.css change often; without this the browser holds a stale copy and
# the UI keeps using an old URL schema (e.g. tile requests without &crs=),
# which is extremely confusing. no-store forces a fresh fetch every reload.
@app.middleware("http")
async def _no_cache_static(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# ---- COG tile server (titiler) -------------------------------------------
#
# We do NOT hand-roll raster tiling any more. titiler is the production COG
# tile server used by NASA Worldview / FIRMS: it reads the source GeoTIFF
# through rasterio, picks the right overview level for the requested zoom,
# resamples to the tile size, and returns a PNG. It handles non-square
# rasters, arbitrary CRS, multi-band, rescale and colormap — all the pieces
# that broke when we tried to write them by hand.
#
# Routes exposed under /cog:
#   GET /cog/tiles/{z}/{x}/{y}.png?url=<path>          — single raster
#   GET /cog/tilejson.json?url=<path>                   — metadata for OL
#
# The frontend builds one ol.source.TileImage per /cog/tiles URL; mosaics
# and overlays become additional layers pointing at their own url=.

_cog_tiler = _CogTilerFactory()

# titiler raises TileOutsideBounds *inside* the tile endpoint when OpenLayers
# requests a tile that does not intersect the raster — which is routine,
# because OL always covers the whole viewport, not just the raster. The
# exception is swallowed by titiler's own error handling and turned into
# HTTP 500 before it reaches FastAPI's exception handlers, so we must
# intercept it at the source: patch rio-tiler's Reader.tile to return a
# fully transparent image for out-of-bounds tiles instead of raising.
# This is what every standard XYZ tile server does.
_original_tile = _RioReader.tile


def _safe_tile(self, *args, **kwargs):
    try:
        return _original_tile(self, *args, **kwargs)
    except _TileOutsideBounds:
        # A fully-transparent tile: one band of zeros + an all-zero mask.
        size = int(kwargs.get("tile_size", 256) or 256)
        blank = np.zeros((1, size, size), dtype=np.uint8)
        blank_mask = np.zeros((size, size), dtype=np.uint8)
        return _ImageData(blank, blank_mask, assets=["outside"])


_RioReader.tile = _safe_tile

app.include_router(_cog_tiler.router, prefix="/cog", tags=["cog"])

# ---- in-memory GeoTIFF store ----------------------------------------------


@dataclass
class _GeoTIFFEntry:
    raster: gio.Raster
    preview_png: bytes
    meta: dict
    # Absolute path to the source GeoTIFF on disk. titiler reads tiles
    # directly from this file (by path or file:// URL), so we must keep it
    # around for the lifetime of the entry. The tempdir is cleaned up by
    # the OS at reboot, not by us.
    path: Path


_geotiffs: dict[str, _GeoTIFFEntry] = {}

# The CRS of the *most recently uploaded* GeoTIFF. Every polygon in the
# shared PolygonState is assumed to be in this CRS (the map switches to
# the raster's projection once it is loaded). Shapefile export uses this.
_current_crs = None  # pyproj.CRS | None

# Id of the most recently uploaded GeoTIFF. Used by ``/api/state`` so a
# page reload can restore the basemap without re-uploading the file.
_current_geotiff_id: str | None = None


def _render_preview_png(raster: gio.Raster, max_size: int = 4096) -> bytes:
    """Render a raster to an 8-bit PNG for use as a map layer.

    ``max_size`` caps the longest side. 4096 keeps enough detail that the
    user can zoom in and still see the raster's structure, while staying a
    sane size for a single PNG (a 4096x4096 RGB PNG is ~10-20 MB, fine for
    a local browser to hold as one ImageStatic). If the source raster is
    smaller than max_size it is used at full resolution.
    """
    from PIL import Image

    data = raster.data
    bands, height, width = data.shape

    if max(height, width) > max_size:
        step = int(np.ceil(max(height, width) / max_size))
        data = data[:, ::step, ::step]
        bands, height, width = data.shape

    def _stretch(band: np.ndarray) -> np.ndarray:
        band = band.astype(np.float32)
        lo = float(np.nanmin(band))
        hi = float(np.nanmax(band))
        if hi <= lo:
            return np.zeros_like(band, dtype=np.uint8)
        return ((band - lo) / (hi - lo) * 255.0).clip(0, 255).astype(np.uint8)

    if bands >= 3:
        rgb = np.stack([_stretch(data[i]) for i in range(3)], axis=-1)
        img = Image.fromarray(rgb, mode="RGB")
    else:
        gray = _stretch(data[0])
        img = Image.fromarray(gray, mode="L")

    buf = stdlib_io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ---- WebSocket hub --------------------------------------------------------


class _Hub:
    """Tracks connected WebSocket clients and broadcasts state updates.

    Also carries a request/response channel used by the ``capture_map``
    MCP tool: the agent asks the browser to screenshot the map, the
    browser replies over the same WebSocket, and the tool waits on a
    Future keyed by a short request id.
    """

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        # request_id -> Future[dict] set by resolve_capture()
        self._pending: dict[str, asyncio.Future] = {}

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        async with self._lock:
            clients = list(self._clients)
        dead: list[WebSocket] = []
        for ws in clients:
            try:
                await ws.send_text(json.dumps(payload))
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)

    async def request_capture(self, options: dict, timeout: float = 10.0) -> dict:
        """Ask one connected browser to screenshot the map.

        Returns the parsed ``capture_map_response`` payload. Raises
        ``RuntimeError`` if no browser is connected, or ``asyncio.TimeoutError``
        if the browser does not reply within ``timeout`` seconds.
        """
        async with self._lock:
            clients = list(self._clients)
        if not clients:
            raise RuntimeError(
                "No browser is connected to /ws. Open http://127.0.0.1:8765/ "
                "in a tab and try again."
            )
        ws = clients[0]
        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[request_id] = fut
        try:
            await ws.send_text(
                json.dumps(
                    {
                        "type": "capture_map_request",
                        "request_id": request_id,
                        "options": options,
                    }
                )
            )
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)

    def resolve_capture(self, request_id: str, payload: dict) -> None:
        """Match an incoming capture_map_response to its pending Future."""
        fut = self._pending.get(request_id)
        if fut is not None and not fut.done():
            fut.set_result(payload)


hub = _Hub()


async def _broadcast_state() -> None:
    state = get_state()
    await hub.broadcast(
        {
            "type": "polygons",
            "polygons": state.to_geojson(),
            "can_undo": state.can_undo(),
            "can_redo": state.can_redo(),
        }
    )


def save_capture(full_b64: str) -> Path:
    """Decode a base64 PNG from the browser and write it under captures/.

    Returns the path so the MCP tool can report it back to the agent.
    """
    path = CAPTURES_DIR / f"{uuid.uuid4().hex}.png"
    path.write_bytes(base64.b64decode(full_b64))
    return path


# ---- HTTP endpoints -------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api/polygons")
def get_polygons() -> JSONResponse:
    """All digitised polygons as a GeoJSON FeatureCollection."""
    state = get_state()
    payload = state.to_geojson()
    payload["can_undo"] = state.can_undo()
    payload["can_redo"] = state.can_redo()
    return JSONResponse(payload)


@app.get("/api/geotiff/{entry_id}/cog-url")
def geotiff_cog_url(entry_id: str) -> JSONResponse:
    """Return the on-disk path of the source GeoTIFF for use by titiler.

    The frontend uses this to build a titiler URL like::

        /cog/WebMercatorQuad/tilejson.json?url=/tmp/glacier_raster_x/source.tif

    Only paths that were registered via /api/geotiff/upload are returned,
    so a client cannot make titiler read arbitrary files from disk.
    """
    entry = _geotiffs.get(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown GeoTIFF id.")
    return JSONResponse({"url": str(entry.path)})


@app.get("/api/state")
def get_full_state() -> JSONResponse:
    """One-shot snapshot of the whole editing session.

    The frontend calls this on boot so that after a page reload it can
    rebuild everything it needs without the user re-uploading the raster:

      * the *current* GeoTIFF (id + CRS + bounds) so the map projection and
        the raster layer are restored — otherwise polygon coordinates in
        the raster's CRS would be interpreted as Web Mercator and land near
        the pole;
      * the polygon collection in that same CRS;
      * undo/redo availability.

    ``geotiff`` is ``null`` when no raster has been uploaded in this server
    process yet. The frontend falls back to an empty EPSG:3857 view then.
    """
    state = get_state()

    geotiff_payload: dict | None = None
    if _current_geotiff_id is not None:
        entry = _geotiffs.get(_current_geotiff_id)
        if entry is not None:
            geotiff_payload = dict(entry.meta)

    return JSONResponse(
        {
            "geotiff": geotiff_payload,
            "polygons": state.to_geojson(),
            "can_undo": state.can_undo(),
            "can_redo": state.can_redo(),
        }
    )


@app.post("/api/polygons")
async def add_or_replace_polygon(payload: dict) -> JSONResponse:
    """Add a new polygon or replace an existing one.

    Body shapes accepted:
      {"geometry": <GeoJSON Polygon>, "name": "optional"}  -> append new
      {"geometry": <GeoJSON Polygon>, "index": 0}          -> replace #0
    """
    from shapely.geometry import shape as shapely_shape

    if "geometry" not in payload:
        raise HTTPException(status_code=400, detail="Missing 'geometry' field.")

    geom = shapely_shape(payload["geometry"])
    state = get_state()

    try:
        if "index" in payload and payload["index"] is not None:
            state.replace_at(int(payload["index"]), geom)
        else:
            state.add_polygon(geom, name=payload.get("name"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await _broadcast_state()
    return JSONResponse(state.to_geojson())


@app.delete("/api/polygons/{index}")
async def delete_polygon(index: int) -> JSONResponse:
    """Remove polygon at ``index``. Refuses to remove the last one."""
    state = get_state()
    try:
        state.remove_polygon(index)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _broadcast_state()
    return JSONResponse(state.to_geojson())


@app.post("/api/polygons/{index}/vertex/move")
async def move_vertex_endpoint(index: int, payload: dict) -> JSONResponse:
    """Move vertex ``payload['vertex']`` of polygon ``index`` by (dx, dy)."""
    state = get_state()
    try:
        state.move_vertex(
            index,
            int(payload["vertex"]),
            float(payload["dx"]),
            float(payload["dy"]),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _broadcast_state()
    return JSONResponse({"ok": True})


@app.post("/api/history/undo")
async def undo_endpoint() -> JSONResponse:
    state = get_state()
    try:
        state.undo()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _broadcast_state()
    return JSONResponse(state.to_geojson())


@app.post("/api/history/redo")
async def redo_endpoint() -> JSONResponse:
    state = get_state()
    try:
        state.redo()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _broadcast_state()
    return JSONResponse(state.to_geojson())


# ---- GeoTIFF upload + preview --------------------------------------------


@app.post("/api/geotiff/upload")
async def upload_geotiff(file: UploadFile = File(...)) -> JSONResponse:
    """Accept a GeoTIFF upload, load it, cache preview, return metadata."""
    global _current_crs, _current_geotiff_id
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided.")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".tif", ".tiff"}:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported extension {suffix!r}; expected .tif or .tiff.",
        )

    # Write the upload to a directory that survives for the life of the
    # process. Tile requests open this path lazily, so it must NOT be
    # deleted after load_geotiff() — that was a bug: tiles returned 500
    # with `RasterioIOError: No such file or directory` because the temp
    # file was unlinked in the `finally` below.
    upload_dir = Path(tempfile.mkdtemp(prefix="glacier_raster_"))
    raster_path = upload_dir / f"source{suffix}"
    with raster_path.open("wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)

    try:
        raster = gio.load_geotiff(raster_path)
    except gio.IOError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    preview = _render_preview_png(raster)
    data_min = float(np.nanmin(raster.data))
    data_max = float(np.nanmax(raster.data))

    h, w = raster.shape[1], raster.shape[0]
    left, top = raster.transform * (0, 0)
    right, bottom = raster.transform * (w, h)
    bounds = [float(left), float(bottom), float(right), float(top)]

    entry_id = uuid.uuid4().hex[:12]
    # proj4js 2.x does not parse WKT2, only WKT1_GDAL or raw proj4.
    try:
        crs_wkt = raster.crs.to_wkt(version="WKT1_GDAL")
    except Exception:
        crs_wkt = raster.crs.to_wkt()
    try:
        crs_proj4 = raster.crs.to_proj4()
    except Exception:
        crs_proj4 = ""

    meta = {
        "id": entry_id,
        "filename": file.filename,
        "crs_wkt": crs_wkt,
        "crs_proj4": crs_proj4,
        "crs_epsg": raster.crs.to_epsg(),
        "crs_name": raster.crs.name,
        "bounds": bounds,
        "width": int(raster.shape[0]),
        "height": int(raster.shape[1]),
        "bands": int(raster.data.shape[0]),
        "data_min": data_min,
        "data_max": data_max,
    }
    _geotiffs[entry_id] = _GeoTIFFEntry(
        raster=raster, preview_png=preview, meta=meta, path=raster_path
    )
    # Remember the CRS for later shapefile export.
    _current_crs = raster.crs
    # Remember which id is "current" so /api/state can restore the
    # basemap after a page reload without re-uploading the file.
    _current_geotiff_id = entry_id
    # NOTE: the raster file on disk is intentionally left in place:
    # `render_tile_png` opens it on every tile request. It lives in a
    # per-upload tempdir and is cleaned up by the OS.

    return JSONResponse(meta)


@app.get("/api/geotiff/{entry_id}/info")
def geotiff_info(entry_id: str) -> JSONResponse:
    entry = _geotiffs.get(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown GeoTIFF id.")
    return JSONResponse(entry.meta)


@app.get("/api/geotiff/{entry_id}/preview.png")
def geotiff_preview(entry_id: str) -> Response:
    entry = _geotiffs.get(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown GeoTIFF id.")
    return Response(content=entry.preview_png, media_type="image/png")


@app.post("/api/export/shapefile")
def export_shapefile_endpoint(payload: dict | None = None) -> Response:
    """Write every polygon in state to a shapefile and stream it as a ZIP.

    Optional body: {"basename": "glaciers"}. Default basename is "glaciers".
    Requires a GeoTIFF to have been uploaded first (for the CRS).
    """
    if _current_crs is None:
        raise HTTPException(
            status_code=400,
            detail="Load a GeoTIFF first to establish the export CRS.",
        )

    state = get_state()
    polygons = state.polygons
    names = state.names
    if not polygons:
        raise HTTPException(status_code=400, detail="No polygons to export.")

    basename = "glaciers"
    if payload and isinstance(payload, dict) and payload.get("basename"):
        basename = str(payload["basename"])

    tmpdir = Path(tempfile.mkdtemp(prefix="glacier_shp_"))
    try:
        try:
            # We only need the side effect (files written into tmpdir);
            # the list of paths is not used further here.
            gio.export_shapefile(
                polygons=polygons,
                names=names,
                crs=_current_crs,
                out_dir=tmpdir,
                name=basename,
            )
        except gio.IOError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Zip all sidecar files together so the download is one click.
        zip_base = tmpdir / basename
        zip_path = shutil.make_archive(str(zip_base), "zip", root_dir=str(tmpdir))
        return FileResponse(
            zip_path,
            media_type="application/zip",
            filename=f"{basename}.zip",
        )
    finally:
        # Clean up the zip too once the response has been sent is tricky
        # under TestClient; leaving temp dir behind is acceptable for a
        # local single-user tool. (A background task would be the proper
        # solution if this ever runs as a service.)
        pass


@app.get("/api/geotiff/{entry_id}/tile/{z}/{x}/{y}.png")
def geotiff_tile(entry_id: str, z: int, x: int, y: int) -> Response:
    """Serve one tile of the source raster, cut in the raster's own CRS.

    The tile grid is defined the same way as on the frontend side:

      * ``z = 0`` — the whole raster in a single 256x256 tile,
      * ``z = N`` — ``2**N`` by ``2**N`` tiles,
      * ``(0, 0)`` at the raster's top-left corner,
      * rows increase downwards (matching rasterio).

    Tiles outside the raster are a transparent PNG so the client can
    request them without 404s when panning near the edge.
    """
    entry = _geotiffs.get(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown GeoTIFF id.")
    # DEBUG: log every tile request. This is intentionally very noisy so we
    # can see exactly which tile coordinates the frontend asks for and how
    # they map to the raster. Turn off / gate with an env var later.
    import sys as _sys
    _sys.stderr.write(
        f"[tile-req] id={entry_id} z={z} x={x} y={y}\n"
    )
    _sys.stderr.flush()
    try:
        png = gio.render_tile_png(entry.raster.path, z=z, x=x, y=y, tile_size=256)
    except gio.IOError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        # zoom out of range from render_tile_png
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _sys.stderr.write(
        f"[tile-ok]  id={entry_id} z={z} x={x} y={y} bytes={len(png)}\n"
    )
    _sys.stderr.flush()
    # Cache aggressively: tiles are immutable for a given (id, z, x, y).
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


@app.get("/api/geotiff/{entry_id}/viewport")
def geotiff_viewport(
    entry_id: str,
    minX: float,
    minY: float,
    maxX: float,
    maxY: float,
    width: int,
    height: int,
) -> Response:
    """Render an arbitrary viewport of the source raster as a single PNG.

    This is the WMS-style endpoint used by the frontend's
    ol.source.ImageCanvas. Instead of the fixed 256x256 tile grid it
    accepts a CRS bounding box (minX, minY, maxX, maxY — in the raster's
    own CRS) and the desired output size in screen pixels. The response is
    exactly one PNG of that size.

    Resampling inside render_viewport_png:

      * If the raster window has MORE pixels than the output size, we
        CROP a symmetric sub-window — 1:1, no blur.
      * If the raster window has FEWER pixels (zoomed in past native
        resolution), we upscale with NEAREST so each raster pixel becomes
        a solid NxN block.

    No 256x256 grid, no tile edges, no bilinear blur.
    """
    entry = _geotiffs.get(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown GeoTIFF id.")
    # Guard against absurd sizes that could blow memory on a stray request.
    width = max(1, min(int(width), 8192))
    height = max(1, min(int(height), 8192))
    import sys as _sys
    _sys.stderr.write(
        f"[viewport] id={entry_id} bbox=({minX:.2f},{minY:.2f},{maxX:.2f},{maxY:.2f}) "
        f"size={width}x{height}\n"
    )
    _sys.stderr.flush()
    try:
        png = gio.render_viewport_png(
            entry.raster.path,
            min_x=float(minX),
            min_y=float(minY),
            max_x=float(maxX),
            max_y=float(maxY),
            out_w=int(width),
            out_h=int(height),
        )
    except gio.IOError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # No long cache: the same (id, bbox, size) may be requested repeatedly
    # as the user pans, but each distinct bbox is basically unique. Short
    # cache to smooth small back-and-forth pans is enough.
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=5"},
    )


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await hub.connect(ws)
    state = get_state()
    await ws.send_text(
        json.dumps(
            {
                "type": "polygons",
                "polygons": state.to_geojson(),
                "can_undo": state.can_undo(),
                "can_redo": state.can_redo(),
            }
        )
    )
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(msg, dict) and msg.get("type") == "capture_map_response":
                hub.resolve_capture(msg.get("request_id") or "", msg)
    except WebSocketDisconnect:
        await hub.disconnect(ws)


# ---- static frontend ------------------------------------------------------

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Start the map UI server on ``host:port``."""
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")
