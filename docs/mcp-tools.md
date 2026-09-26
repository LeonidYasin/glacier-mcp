# MCP tools (planned)

All tools operate on the single in-memory polygon. Coordinates are in the CRS of the loaded GeoTIFF.

## Read

| Tool | Arguments | Returns |
|------|-----------|---------|
| `get_polygon` | - | GeoJSON `Polygon` in the working CRS |
| `get_vertices` | - | `[{index, x, y}, ...]` |

## Edit

| Tool | Arguments | Notes |
|------|-----------|-------|
| `move_vertex` | `index: int, dx: float, dy: float` | units = CRS units (metres for UTM) |
| `add_vertex` | `index: int, coords: [x, y]` | inserts *before* `index` |
| `delete_vertex` | `index: int` | refuses if fewer than 3 vertices would remain |
| `translate_polygon` | `dx: float, dy: float` | moves all vertices |
| `smooth_polygon` | `tolerance: float` | Chaikin or Douglas-Peucker, TBD |

## History

| Tool | Arguments |
|------|-----------|
| `undo` | - |
| `redo` | - |

## Export

| Tool | Arguments |
|------|-----------|
| `export_shapefile` | `path: str` - writes `.shp/.shx/.dbf/.prj/.cpg` in the original CRS |
