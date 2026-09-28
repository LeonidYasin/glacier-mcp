"""Multi-polygon state: a collection of glacier polygons + edit history.

Single source of truth for both the mouse-driven editor and the MCP tools.
Unlike the earlier single-polygon version, this state holds a *list* of
polygons (one per glacier), each with a name. Every mutation goes through
:meth:`PolygonState.apply`, so there is exactly one code path that pushes
onto the undo stack.

The undo/redo history is **global** (not per-polygon): adding a glacier,
removing one, or editing a vertex of any glacier all count as one step.
This matches the user's mental model when digitising several glaciers in
one session.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable

from shapely.geometry import Polygon

from . import geometry as geom


class StateError(RuntimeError):
    """Raised on invalid operations (bad index, empty undo/redo stack)."""


DEFAULT_HISTORY = 100


class PolygonState:
    """Holds a list of glacier polygons and a bounded undo/redo stack.

    A snapshot of ``(polygons, names)`` is pushed onto the undo stack for
    every mutation. Keeping both lists in the snapshot means an add or
    remove is a single undo step that restores names as well.
    """

    def __init__(
        self,
        polygon: Polygon | None = None,
        history_size: int = DEFAULT_HISTORY,
    ) -> None:
        # By default we start EMPTY — the user draws the first glacier. An
        # initial polygon is only added if explicitly passed (tests rely on
        # this to seed a square). Earlier versions auto-created a 1x1 square
        # at the origin, which showed up as a phantom glacier on first run
        # and could not be deleted because it was the last one.
        self._polygons: list[Polygon] = []
        self._names: list[str] = []
        if polygon is not None:
            self._polygons.append(polygon)
            self._names.append("glacier_1")
        self._undo: deque[tuple[list[Polygon], list[str]]] = deque(maxlen=history_size)
        self._redo: deque[tuple[list[Polygon], list[str]]] = deque(maxlen=history_size)

    # ---- read -------------------------------------------------------------

    @property
    def polygons(self) -> list[Polygon]:
        return list(self._polygons)

    @property
    def names(self) -> list[str]:
        return list(self._names)

    def __len__(self) -> int:
        return len(self._polygons)

    def get_polygon(self, index: int = 0) -> Polygon:
        self._check_index(index)
        return self._polygons[index]

    # Kept for backwards compatibility with tests and callers that assume
    # a single polygon: returns the first one.
    @property
    def polygon(self) -> Polygon:
        return self._polygons[0]

    def get_vertices(self, polygon_index: int = 0) -> list[geom.Vertex]:
        return geom.vertices(self.get_polygon(polygon_index))

    def to_geojson(self) -> dict:
        """Return all polygons as a single GeoJSON FeatureCollection.

        Each Feature carries ``index`` and ``name`` properties so a consumer
        can round-trip back into this state and so the shapefile export has
        something to put in the attribute table.
        """
        features = []
        for i, poly in enumerate(self._polygons):
            features.append(
                {
                    "type": "Feature",
                    "properties": {"index": i, "name": self._names[i]},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [list(c) for c in poly.exterior.coords],
                        ],
                    },
                }
            )
        return {"type": "FeatureCollection", "features": features}

    # ---- write ------------------------------------------------------------

    def _snapshot(self) -> tuple[list[Polygon], list[str]]:
        return list(self._polygons), list(self._names)

    def _check_index(self, index: int) -> None:
        n = len(self._polygons)
        if not -n <= index < n:
            raise StateError(f"Polygon index {index} out of range (0..{n - 1}).")

    def apply(self, fn: Callable[[list[Polygon], list[str]], None]) -> None:
        """Apply a mutation and push the previous state onto undo.

        ``fn`` receives mutable copies of the polygons and names lists and
        is expected to mutate them in place. If ``fn`` raises, state is
        left untouched (we only commit after it returns normally).
        """
        new_polys = list(self._polygons)
        new_names = list(self._names)
        fn(new_polys, new_names)
        if new_polys == self._polygons and new_names == self._names:
            return
        self._undo.append(self._snapshot())
        self._redo.clear()
        self._polygons = new_polys
        self._names = new_names

    def add_polygon(self, polygon: Polygon, name: str | None = None) -> int:
        """Append a new polygon. Returns its index."""
        if polygon.is_empty or not polygon.is_valid:
            raise StateError("Refusing to add an empty or invalid polygon.")

        result_index: list[int] = []

        def _mutate(polys: list[Polygon], names: list[str]) -> None:
            polys.append(polygon)
            names.append(name if name is not None else f"glacier_{len(polys)}")
            result_index.append(len(polys) - 1)

        self.apply(_mutate)
        return result_index[0]

    def remove_polygon(self, index: int = 0) -> None:
        """Remove polygon at ``index``. Allowed even for the last polygon.

        An empty state is legitimate — the user may want to start over, and
        the map panel already handles the empty case. The earlier guard that
        refused to remove the last polygon was a single-polygon leftover.
        """
        self._check_index(index)

        def _mutate(polys: list[Polygon], names: list[str]) -> None:
            del polys[index]
            del names[index]

        self.apply(_mutate)

    def replace_at(self, index: int, polygon: Polygon) -> None:
        """Replace one polygon (used after a mouse draw that edits it)."""
        self._check_index(index)

        def _mutate(polys: list[Polygon], _names: list[str]) -> None:
            polys[index] = polygon

        self.apply(_mutate)

    # Kept for backwards compatibility: replaces the first polygon.
    def replace(self, polygon: Polygon) -> Polygon:
        self.replace_at(0, polygon)
        return self._polygons[0]

    def move_vertex(
        self, polygon_index: int, index: int, dx: float, dy: float
    ) -> None:
        self._check_index(polygon_index)

        def _mutate(polys: list[Polygon], _names: list[str]) -> None:
            polys[polygon_index] = geom.move_vertex(polys[polygon_index], index, dx, dy)

        self.apply(_mutate)

    def add_vertex(self, polygon_index: int, index: int, coords: geom.Vertex) -> None:
        self._check_index(polygon_index)

        def _mutate(polys: list[Polygon], _names: list[str]) -> None:
            polys[polygon_index] = geom.add_vertex(polys[polygon_index], index, coords)

        self.apply(_mutate)

    def delete_vertex(self, polygon_index: int, index: int) -> None:
        self._check_index(polygon_index)

        def _mutate(polys: list[Polygon], _names: list[str]) -> None:
            polys[polygon_index] = geom.delete_vertex(polys[polygon_index], index)

        self.apply(_mutate)

    def translate(self, polygon_index: int, dx: float, dy: float) -> None:
        self._check_index(polygon_index)

        def _mutate(polys: list[Polygon], _names: list[str]) -> None:
            polys[polygon_index] = geom.translate_polygon(polys[polygon_index], dx, dy)

        self.apply(_mutate)

    def smooth(self, polygon_index: int, tolerance: float = 0.0) -> None:
        self._check_index(polygon_index)

        def _mutate(polys: list[Polygon], _names: list[str]) -> None:
            polys[polygon_index] = geom.smooth_polygon(polys[polygon_index], tolerance)

        self.apply(_mutate)

    # ---- history ----------------------------------------------------------

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> None:
        if not self._undo:
            raise StateError("Nothing to undo.")
        self._redo.append(self._snapshot())
        self._polygons, self._names = self._undo.pop()

    def redo(self) -> None:
        if not self._redo:
            raise StateError("Nothing to redo.")
        self._undo.append(self._snapshot())
        self._polygons, self._names = self._redo.pop()
