# Architecture

## One process, two servers

```
                +----------------------------------+
                |        python -m glacier_mcp     |
                |                                  |
  browser  <--->  FastAPI  :8765  (HTTP + WS)       |
                |                                  |
  agent    <--->  MCP      :8766  (streamable-http) |
                +----------------------------------+
```

- **FastAPI (port 8765)** serves the OpenLayers page and a WebSocket endpoint `/ws`.
- **MCP (port 8766)** exposes the tool surface to any MCP client.
- Both share a single in-memory `PolygonState` instance.

## Polygon state is the single source of truth

Every edit - whether it comes from a mouse drag in the browser or from an agent calling `move_vertex` - goes through the same code path:

1. Compute a new shapely geometry.
2. Push the previous state onto the undo stack.
3. Broadcast the new state over the WebSocket to all connected clients.

There is no separate "agent edit" vs "user edit" state. One polygon, one history.

## Coordinate reference systems

- The GeoTIFF is loaded with its **original CRS** and an affine `transform`.
- The OpenLayers map is configured with a view in that CRS (OpenLayers supports arbitrary projections; `proj4js` if the CRS is not in the built-in list).
- Web Mercator (EPSG:3857) is **not** used internally to avoid precision loss for polar glacier work.
- `export_shapefile` writes `.prj` from the original CRS.

## Why OpenLayers and not Leaflet

- Native support for arbitrary projections (polar stereographic, UTM zones).
- Built-in `Modify`, `Snap`, `Draw` interactions.
- No plugin zoo needed for vertex editing and reprojection.

## Why streamable-http and not stdio

- One process serves both the map and the agent.
- stdio MCP would require the map UI to run as a separate service and pass geometry back and forth.
- streamable-http is the transport modern MCP clients prefer.
