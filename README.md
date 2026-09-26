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
2. Load your GeoTIFF (satellite image of a glacier)
3. Draw a polygon around the glacier with the mouse
4. Connect your MCP client (Claude Desktop / Cursor / DeepSeek++ / etc.) to http://localhost:8766
5. Ask the agent: *"move the western boundary 50 meters east"*

## MCP tools

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
| `export_shapefile`   | Save to `.shp` + `.shx` + `.dbf` + `.prj` + `.cpg` |

## Status

Early skeleton. See `docs/architecture.md` and `docs/mcp-tools.md`.

## License

MIT - see [LICENSE](LICENSE).
