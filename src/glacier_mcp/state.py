"""Polygon state: current geometry + edit history (undo / redo).

Single source of truth for both the mouse-driven editor and the MCP tools.
Every mutation goes through :meth:`PolygonState.apply`, so there is exactly
one code path that pushes onto the undo stack.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable

from shapely.geometry import Polygon

from . import geometry as geom


class StateError(RuntimeError):
    """Raised on invalid history operations (empty undo/redo stack)."""


DEFAULT_HISTORY = 100


class PolygonState:
    """Holds the working polygon and a bounded undo/redo stack.

    Parameters
    ----------
    polygon:
        Initial polygon. If ``None`` a default small square around the
        origin is created; the UI is expected to replace it before any
        real work.
    history_size:
        Maximum number of undo steps kept in memory.
    """

    def __init__(
        self,
        polygon: Polygon | None = None,
        history_size: int = DEFAULT_HISTORY,
    ) -> None:
        if polygon is None:
            polygon = Polygon([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)])
        self._polygon: Polygon = polygon
        self._undo: deque[Polygon] = deque(maxlen=history_size)
        self._redo: deque[Polygon] = deque(maxlen=history_size)

    # ---- read -------------------------------------------------------------

    @property
    def polygon(self) -> Polygon:
        return self._polygon

    def get_vertices(self) -> list[geom.Vertex]:
        return geom.vertices(self._polygon)

    def to_geojson(self) -> dict:
        """Minimal GeoJSON Feature for the current polygon.

        No CRS is embedded - the caller knows which GeoTIFF is loaded.
        """
        return {
            "type": "Feature",
            "properties": {},
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [list(c) for c in self._polygon.exterior.coords],
                ],
            },
        }

    # ---- write ------------------------------------------------------------

    def apply(self, fn: Callable[[Polygon], Polygon]) -> Polygon:
        """Apply a geometry transform, pushing the previous state on undo.

        ``fn`` receives the current polygon and must return a new one. If
        ``fn`` raises, state is left untouched.
        """
        new_poly = fn(self._polygon)
        if new_poly is self._polygon or new_poly.equals(self._polygon):
            return self._polygon
        self._undo.append(self._polygon)
        self._redo.clear()
        self._polygon = new_poly
        return self._polygon

    def replace(self, polygon: Polygon) -> Polygon:
        """Replace the whole polygon (e.g. after a mouse draw)."""
        return self.apply(lambda _old: polygon)

    def move_vertex(self, index: int, dx: float, dy: float) -> Polygon:
        return self.apply(lambda p: geom.move_vertex(p, index, dx, dy))

    def add_vertex(self, index: int, coords: geom.Vertex) -> Polygon:
        return self.apply(lambda p: geom.add_vertex(p, index, coords))

    def delete_vertex(self, index: int) -> Polygon:
        return self.apply(lambda p: geom.delete_vertex(p, index))

    def translate(self, dx: float, dy: float) -> Polygon:
        return self.apply(lambda p: geom.translate_polygon(p, dx, dy))

    def smooth(self, tolerance: float = 0.0) -> Polygon:
        return self.apply(lambda p: geom.smooth_polygon(p, tolerance))

    # ---- history ----------------------------------------------------------

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> Polygon:
        if not self._undo:
            raise StateError("Nothing to undo.")
        self._redo.append(self._polygon)
        self._polygon = self._undo.pop()
        return self._polygon

    def redo(self) -> Polygon:
        if not self._redo:
            raise StateError("Nothing to redo.")
        self._undo.append(self._polygon)
        self._polygon = self._redo.pop()
        return self._polygon
