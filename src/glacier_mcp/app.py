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
import io as stdlib_io
import json
import shutil
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


@dataclass
class _GeoTIFFEntry:
    raster: gio.Raster
    preview_png: bytes
    meta: dict


_geotiffs: dict[str, _GeoTIFFEntry] = {}

# The CRS of the *most recently uploaded* GeoTIFF. Every polygon in the
# shared PolygonState is assumed to be in this CRS (the map switches to
# the raster's projection once it is loaded). Shapefile export uses this.
_current_crs = None  # pyproj.CRS | None


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
    global _current_crs
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided.")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".tif", ".tiff"}:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported extension {suffix!r}; expected .tif or .tiff.",
        )

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
        _geotiffs[entry_id] = _GeoTIFFEntry(raster=raster, preview_png=preview, meta=meta)
        # Remember the CRS for later shapefile export.
        _current_crs = raster.crs
    finally:
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
