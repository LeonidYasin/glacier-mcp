"""MCP tool definitions.

Each function here is registered on the MCP server in ``server.py``.

Planned tools:
  * get_polygon()        -> GeoJSON
  * get_vertices()       -> list[{index, x, y}]
  * move_vertex(index, dx, dy)
  * add_vertex(index, coords)
  * delete_vertex(index)
  * translate_polygon(dx, dy)
  * smooth_polygon(tolerance)
  * undo()
  * redo()
  * export_shapefile(path)

Placeholder - to be implemented.
"""

from __future__ import annotations
