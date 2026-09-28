"""FastAPI app: serves the OpenLayers UI and the WebSocket bridge.

The UI and the MCP server share a single :class:`PolygonState` via
``server.get_state()``. Any mutation - whether from the mouse or from an
agent calling an MCP tool - triggers a broadcast over ``/ws`` so every
connected browser tab stays in sync.

Also serves GeoTIFF upload + preview so the map can render a real raster
basemap instead of an empty background.
"""

from __future__ import annotations

import asyncio
import io as stdlib_io
import json
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import io as gio
from .server import get_state

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"

app = FastAPI(title="glacier-mcp")

# ---- in-memory GeoTIFF store ----------------------------------------------
#
# Maps an opaque id to the loaded Raster + a pre-rendered PNG preview. This is
# deliberately simple: for a local single-user tool we do not need persistence
# or eviction. If a future version wants multi-user, swap for a disk-backed
# LRU keyed by content hash.


@dataclass
class _GeoTIFFEntry:
    raster: gio.Raster
    preview_png: bytes
    meta: dict


_geotiffs: dict[str, _GeoTIFFEntry] = {}


def _render_preview_png(raster: gio.Raster, max_size: int = 1024) -> bytes:
    """Render a raster to a small 8-bit PNG for use as a map layer.

    Handles 1-band (grayscale), 3-band (RGB) and 4-band (RGBA) inputs. Any
    other band count falls back to using the first band as grayscale. Data is
    normalized min-max per band and stretched to 0..255.
    """
    from PIL import Image

    data = raster.data
    bands, height, width = data.shape

    # Downsample if the raster is large - we only need a preview.
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
    """Tracks connected WebSocket clients and broadcasts state updates."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

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


hub = _Hub()


# ---- HTTP endpoints -------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api/polygon")
def get_polygon() -> JSONResponse:
    """Current polygon as GeoJSON. Used by the frontend on first load."""
    return JSONResponse(get_state().to_geojson())


@app.post("/api/polygon")
async def set_polygon(payload: dict) -> JSONResponse:
    """Replace the polygon (e.g. after a mouse draw in the browser)."""
    from shapely.geometry import shape as shapely_shape

    geom = shapely_shape(payload["geometry"])
    state = get_state()
    state.replace(geom)
    await hub.broadcast({"type": "polygon", "polygon": state.to_geojson()})
    return JSONResponse(state.to_geojson())


@app.post("/api/vertex/move")
async def move_vertex(payload: dict) -> JSONResponse:
    """Move vertex ``index`` by (dx, dy). Used by drag handlers."""
    state = get_state()
    state.move_vertex(int(payload["index"]), float(payload["dx"]), float(payload["dy"]))
    await hub.broadcast({"type": "polygon", "polygon": state.to_geojson()})
    return JSONResponse({"ok": True})


# ---- GeoTIFF upload + preview --------------------------------------------


@app.post("/api/geotiff/upload")
async def upload_geotiff(file: UploadFile = File(...)) -> JSONResponse:
    """Accept a GeoTIFF upload, load it, cache preview, return metadata.

    The file is written to a NamedTemporaryFile, opened by rasterio, then
    deleted. The resulting raster stays in memory keyed by an opaque id.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided.")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".tif", ".tiff"}:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported extension {suffix!r}; expected .tif or .tiff.",
        )

    # Write to a temp file (rasterio needs a real path, not a stream).
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            tmp.write(chunk)
        tmp.close()

        try:
            raster = gio.load_geotiff(tmp.name)
        except gio.IOError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        preview = _render_preview_png(raster)
        data_min = float(np.nanmin(raster.data))
        data_max = float(np.nanmax(raster.data))

        # Compute bounds from transform + shape: (left, bottom, right, top).
        h, w = raster.shape[1], raster.shape[0]
        left, top = raster.transform * (0, 0)
        right, bottom = raster.transform * (w, h)
        bounds = [float(left), float(bottom), float(right), float(top)]

        entry_id = uuid.uuid4().hex[:12]
        # proj4js 2.x does not parse WKT2 (PROJCRS[...]), only WKT1_GDAL
        # (PROJCS[...]) and raw proj4 strings. pyproj's to_wkt() defaults to
        # WKT2 in modern versions, which silently breaks proj4.defs() in the
        # browser. We ship both: WKT (WKT1_GDAL flavour) for reference and a
        # proj4 string that proj4js can consume directly.
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
        _geotiffs[entry_id] = _GeoTIFFEntry(raster=raster, preview_png=preview, meta=meta)
    finally:
        # Always remove the temp file; the raster is already in memory.
        try:
            Path(tmp.name).unlink(missing_ok=True)
        except OSError:
            pass

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


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await hub.connect(ws)
    # Send current state immediately so a fresh client is in sync.
    await ws.send_text(
        json.dumps({"type": "polygon", "polygon": get_state().to_geojson()})
    )
    try:
        while True:
            # We do not expect messages from the client yet; this keeps the
            # connection alive and lets us react to disconnects.
            await ws.receive_text()
    except WebSocketDisconnect:
        await hub.disconnect(ws)


# ---- static frontend ------------------------------------------------------

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Start the map UI server on ``host:port``."""
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")
