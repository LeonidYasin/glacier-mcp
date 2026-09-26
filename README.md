# glacier-mcp

MCP server for interactive glacier mapping.

Renders a GeoTIFF in OpenLayers, lets you draw and edit polygons with the mouse, and exposes every vertex to an AI agent via MCP tools (get / move / add / delete). Exports the final geometry to a shapefile with a correct `.prj`.

## Why

Existing GIS tools (ArcGIS Pro, QGIS) can be automated via MCP, but none of them give an AI agent *direct, per-vertex* control over geometry while you edit the same geometry with your mouse. This project fills that gap: the map and the agent share the same polygon state.

## Architecture

```
+----------------+     WebSocket      +-------------------+     MCP      +-------+
|  OpenLayers UI | <----------------> |  FastAPI backend  | <----------> | Agent |
|  (your mouse)  |                    |  (state + geom)   |              | (LLM) |
+----------------+                    +-------------------+              +-------+
```

- **Backend**: Python 3.11, FastAPI, `mcp` SDK, uvicorn, shapely, pyproj.
- **Frontend**: OpenLayers 10, vanilla JS.
- **I/O**: rasterio (GeoTIFF), geopandas (shapefile).
- **Bridge**: WebSocket (frontend <-> backend), MCP streamable-http (agent <-> backend).

Two ports, one process:

- `http://localhost:8765` - interactive map (open in browser)
- `http://localhost:8766` - MCP endpoint (connect your MCP client here)

## Quick start

```bash
pip install -r requirements.txt
python -m glacier_mcp
```

Then:

1. Open http://localhost:8765
2. Draw a polygon around a glacier with the mouse (GeoTIFF basemap is a follow-up; the map uses an OSM placeholder for now)
3. Connect your MCP client (Claude Desktop / Cursor / DeepSeek++ / etc.) to http://localhost:8766
4. Ask the agent: *"move the western boundary 50 meters east"*

## Python API

```python
from glacier_mcp import io as gio
from shapely.geometry import Polygon

raster = gio.load_geotiff("arctic_dem_58_22.tif")
print(raster.crs, raster.shape)

poly = Polygon([(500_000, 5_000_000), (500_100, 5_000_000), (500_100, 4_999_900)])
written = gio.export_shapefile(poly, raster.crs, "out/", name="glacier")
print("wrote:", [p.name for p in written])
```

The exported shapefile always carries a `.prj` derived from `raster.crs`. Export in EPSG:4326 (lat/lon) is refused — reproject to a metric CRS first.

## MCP tools

All tools operate on the single in-memory polygon. Coordinates are in the CRS of the loaded GeoTIFF.

| Tool                 | What it does                                     |
|----------------------|--------------------------------------------------|
| `get_polygon`        | Return current polygon as GeoJSON                |
| `get_vertices`       | Return vertex list with indices                  |
| `move_vertex`        | Move a vertex by index and offset (dx, dy)       |
| `add_vertex`         | Insert a vertex at a position                    |
| `delete_vertex`      | Remove a vertex by index                         |
| `translate_polygon`  | Move the whole polygon by (dx, dy)               |
| `smooth_polygon`     | Apply smoothing to the boundary                  |
| `undo` / `redo`      | Step through the edit history                    |

`export_shapefile` is exposed as a Python API for now; wiring it as an MCP tool is a follow-up (needs a decision on where the server is allowed to write files).

## Status

Working skeleton. All geometry operations are unit-tested, the MCP server runs, and the OpenLayers UI is wired to the same `PolygonState` via WebSocket. GeoTIFF basemap loading in the UI is next.

## License

MIT - see [LICENSE](LICENSE).
