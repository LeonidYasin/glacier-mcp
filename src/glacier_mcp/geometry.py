"""Pure geometry helpers built on shapely.

All functions take a :class:`shapely.geometry.Polygon` and return a new
Polygon - they never mutate the input. Coordinates are in the CRS units
of the source GeoTIFF (metres for UTM / Polar Stereographic).
"""

from __future__ import annotations

from collections.abc import Sequence

from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry


class GeometryError(ValueError):
    """Raised when an edit would produce an invalid polygon."""


Vertex = tuple[float, float]


def _exterior_coords(polygon: Polygon) -> list[Vertex]:
    """Return exterior ring coordinates without the duplicated closing point."""
    # shapely's .coords includes the first point again at the end; drop it.
    return [(float(x), float(y)) for x, y in list(polygon.exterior.coords)[:-1]]


def _from_coords(coords: Sequence[Vertex]) -> Polygon:
    if len(coords) < 3:
        raise GeometryError("A polygon needs at least 3 vertices.")
    poly = Polygon(coords)
    if not poly.is_valid:
        # Try to recover from minor self-intersections; if that fails, refuse.
        fixed = poly.buffer(0)
        if fixed.is_empty or not isinstance(fixed, Polygon):
            raise GeometryError("Resulting polygon is invalid.")
        return fixed
    return poly


def move_vertex(polygon: Polygon, index: int, dx: float, dy: float) -> Polygon:
    """Move vertex at ``index`` by (dx, dy).

    Index is 0-based and refers to the exterior ring in the order shapely
    returns them. Negative indices are accepted Python-style.
    """
    coords = _exterior_coords(polygon)
    n = len(coords)
    if not -n <= index < n:
        raise GeometryError(f"Vertex index {index} out of range (0..{n - 1}).")
    x, y = coords[index]
    coords[index] = (x + float(dx), y + float(dy))
    return _from_coords(coords)


def add_vertex(polygon: Polygon, index: int, coords: Vertex) -> Polygon:
    """Insert a new vertex *before* position ``index``.

    ``index == len(vertices)`` appends at the end.
    """
    ring = _exterior_coords(polygon)
    n = len(ring)
    if not 0 <= index <= n:
        raise GeometryError(f"Insert index {index} out of range (0..{n}).")
    ring.insert(index, (float(coords[0]), float(coords[1])))
    return _from_coords(ring)


def delete_vertex(polygon: Polygon, index: int) -> Polygon:
    """Remove vertex at ``index``. Refuses if fewer than 3 would remain."""
    ring = _exterior_coords(polygon)
    n = len(ring)
    if n <= 3:
        raise GeometryError("Cannot delete: a polygon needs at least 3 vertices.")
    if not -n <= index < n:
        raise GeometryError(f"Vertex index {index} out of range (0..{n - 1}).")
    del ring[index]
    return _from_coords(ring)


def translate_polygon(polygon: Polygon, dx: float, dy: float) -> Polygon:
    """Move the whole polygon by (dx, dy).

    Uses :func:`shapely.affinity.translate` to avoid rebuilding coordinates
    by hand.
    """
    from shapely.affinity import translate

    moved: BaseGeometry = translate(polygon, xoff=float(dx), yoff=float(dy))
    if not isinstance(moved, Polygon):
        raise GeometryError("Translation produced a non-polygon geometry.")
    return moved


def smooth_polygon(polygon: Polygon, tolerance: float = 0.0) -> Polygon:
    """Smooth the boundary with one pass of Chaikin corner cutting.

    ``tolerance`` is reserved for a future Douglas-Peucker variant and
    currently has no effect.
    """
    ring = _exterior_coords(polygon)
    smoothed: list[Vertex] = []
    n = len(ring)
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % n]
        # Q = 0.75 P0 + 0.25 P1 ; R = 0.25 P0 + 0.75 P1
        smoothed.append((0.75 * x0 + 0.25 * x1, 0.75 * y0 + 0.25 * y1))
        smoothed.append((0.25 * x0 + 0.75 * x1, 0.25 * y0 + 0.75 * y1))
    return _from_coords(smoothed)


def vertices(polygon: Polygon) -> list[Vertex]:
    """Public helper: list exterior vertices without the closing duplicate."""
    return _exterior_coords(polygon)
