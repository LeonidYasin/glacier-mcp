# glacier-mcp — progress & roadmap

> **Last updated:** 2026-09-28
> **Purpose:** single source of truth for "where are we and what's next".
> Read this first when starting a new session.

---

## What this project is

MCP server + browser UI for **interactive glacier mapping**: load a GeoTIFF
as a basemap, draw a polygon per glacier with the mouse, edit vertexes from
the MCP agent, export everything as a shapefile with **one row per glacier**.

**Stack**

| Layer | Tech |
|---|---|
| Backend | Python 3.11, FastAPI, uvicorn, `mcp` SDK (pinned `<2.0`) |
| Raster / GIS | rasterio, pyproj, shapely, geopandas, Pillow |
| Frontend | OpenLayers 10, proj4js, vanilla JS (no build step) |
| CI | GitHub Actions — Ruff + Pytest |

**Layout**

```
src/glacier_mcp/
  state.py     PolygonState — list of polygons + names + global undo/redo
  server.py    FastMCP app exposing 13 MCP tools
  app.py       FastAPI: /api/polygons, /api/geotiff/*, /api/export, /ws
  io.py        load_geotiff, export_shapefile, polygon_to_crs
  geometry.py  pure polygon ops (move/add/delete vertex, translate, smooth)
frontend/
  index.html   top toolbar + bottom polygon panel + map container
  main.js      OpenLayers map, Select/Draw modes, chips, WebSocket sync
  style.css    dark theme, both toolbars, map insets
```

---

## Done

- [x] **Multi-polygon state** — a list of `Polygon` + names, global undo/redo.
- [x] **13 MCP tools** — `get_polygon`, `get_vertices`, `move_vertex`,
      `add_vertex`, `delete_vertex`, `translate_polygon`, `smooth_polygon`,
      `undo`, `redo`, `list_polygons`, `get_polygon_collection`,
      `add_polygon`, `remove_polygon`.
- [x] **GeoTIFF upload** — `POST /api/geotiff/upload`, in-memory store,
      preview PNG up to 4096 px, `crs_wkt` + `crs_proj4` so proj4js can
      register arbitrary CRSs.
- [x] **Shapefile export** — one row per polygon in the `.dbf`
      (`fid`, `name`), `.prj` from the raster CRS.
- [x] **Select / Draw modes** — the two interactions are **not** active at
      once; click selects, `Draw new polygon` (or `N`) enters draw mode,
      `Esc` cancels, double-click ends and returns to Select.
- [x] **Bottom polygon panel** — chips per glacier, click to select,
      Delete Selected button, horizontal scroll when many.
- [x] **Green CI** — Ruff + Pytest, `mcp>=1.0.0,<2.0.0` pin.

## Next (ordered)

1. **Fix phantom glacier on start** *(in progress)*
   `PolygonState.__init__` currently seeds a 1×1 square at the origin, so a
   fresh run shows one glacier that cannot be deleted. Change to start
   empty; allow `remove_polygon` on the last polygon.

2. **Tile server** — real pixel resolution
   Today the basemap is a single `ImageStatic` PNG capped at 4096 px, so
   zooming past 1:1 just stretches pixels. Target:
   `GET /api/geotiff/{id}/tile/{z}/{x}/{y}.png`, cutting the **source**
   raster via `rasterio.windows.Window`. Frontend switches to
   `ol.source.TileImage` + a custom `ol.tilegrid.TileGrid` in the raster
   CRS. That is the only way to reach the native resolution of a
   Sentinel-2 scene (~10980×10980), while keeping browser memory bounded
   to the visible tiles (~5 MB).

3. **Lasso / rectangle select** — drag to select several polygons at once.

4. **Rename polygon** — double-click a chip to edit its `name`.

5. **Snap to vertex** while drawing and dragging.

---

## Dev notes / gotchas

- **`mcp` must stay `<2.0`.** In mcp 2.x `FastMCP` was renamed to
  `MCPServer` and moved to `mcp.server.mcpserver`; our `server.py` uses the
  v1 API. The pin lives in `pyproject.toml`.
- **Draw and Modify must not both be active.** `Draw` swallows `mousedown`
  and `singleclick` never fires — that was the "click starts a new
  polygon" bug. Keep `draw.setActive(false)` at boot and toggle it.
- **`user-select: none` on `#map`.** Without it Shift+drag selects text in
  the browser (looks like a lasso but is not).
- **`import` in tests.** The MCP tools live in `glacier_mcp.server`, not in
  the old `glacier_mcp.tools` stub. `tests/test_tools.py` aliases
  `from glacier_mcp import server as tools`.
- **proj4 needs WKT1_GDAL or a proj4 string.** `pyproj.CRS.to_wkt()`
  defaults to WKT2 in modern versions, which proj4js cannot parse. The
  upload endpoint sends `crs_proj4` and a WKT1_GDAL fallback.

## How to run

```bash
git clone https://github.com/LeonidYasin/glacier-mcp
cd glacier-mcp
python -m venv .venv
source .venv/bin/activate          # Windows (MINGW): source .venv/Scripts/activate
pip install -e ".[dev]"
python -m glacier_mcp
```

Open <http://127.0.0.1:8765>. The MCP endpoint is at
<http://127.0.0.1:8766> (streamable HTTP).

Quick checks:

```bash
ruff check src tests
python -m pytest -q
```
