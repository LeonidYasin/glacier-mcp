"""Tests for the MCP tools defined in glacier_mcp.server.

These call the tool functions directly, without going through the HTTP
transport, so they are fast and dependency-free. A separate test would
be needed to verify the wire protocol itself.
"""

from __future__ import annotations

import pytest
from shapely.geometry import Polygon

from glacier_mcp import tools
from glacier_mcp.geometry import GeometryError
from glacier_mcp.state import PolygonState, StateError


def _reset_state(square_side: float = 10.0) -> None:
    """Reset the shared state to a single square, clearing history."""
    state = tools.get_state()
    poly = Polygon(
        [
            (0.0, 0.0),
            (square_side, 0.0),
            (square_side, square_side),
            (0.0, square_side),
        ]
    )
    # Test-only reset: bypass apply() so we do not push an undo entry.
    state._polygons = [poly]  # noqa: SLF001
    state._names = ["glacier_1"]  # noqa: SLF001
    state._undo.clear()  # noqa: SLF001
    state._redo.clear()  # noqa: SLF001


def setup_function(_func) -> None:
    _reset_state()


# ---- read -----------------------------------------------------------------


def test_list_polygons_returns_one_item() -> None:
    items = tools.list_polygons()
    assert len(items) == 1
    assert items[0]["index"] == 0
    assert items[0]["name"] == "glacier_1"
    assert items[0]["vertex_count"] == 4


def test_get_polygon_collection_shape() -> None:
    gj = tools.get_polygon_collection()
    assert gj["type"] == "FeatureCollection"
    assert len(gj["features"]) == 1


def test_get_polygon_returns_single_feature() -> None:
    gj = tools.get_polygon(0)
    assert gj["type"] == "Feature"
    assert gj["geometry"]["type"] == "Polygon"
    ring = gj["geometry"]["coordinates"][0]
    assert ring[0] == ring[-1]


def test_get_vertices_returns_indices() -> None:
    verts = tools.get_vertices(0)
    assert [v["index"] for v in verts] == [0, 1, 2, 3]
    assert verts[0] == {"index": 0, "x": 0.0, "y": 0.0}


# ---- edit -----------------------------------------------------------------


def test_move_vertex_returns_new_vertices() -> None:
    out = tools.move_vertex(0, 1.0, 2.0, polygon_index=0)
    assert out["ok"] is True
    assert out["vertices"][0] == {"index": 0, "x": 1.0, "y": 2.0}


def test_add_vertex_inserts_before_index() -> None:
    out = tools.add_vertex(1, 5.0, -1.0, polygon_index=0)
    assert out["ok"] is True
    assert out["vertices"][1] == {"index": 1, "x": 5.0, "y": -1.0}


def test_delete_vertex_refuses_when_only_three_left() -> None:
    _reset_state()
    tools.delete_vertex(0, polygon_index=0)  # now 3 vertices
    with pytest.raises(GeometryError):
        tools.delete_vertex(0, polygon_index=0)


def test_translate_polygon_moves_all() -> None:
    out = tools.translate_polygon(100.0, 50.0, polygon_index=0)
    assert out["ok"] is True
    verts = tools.get_vertices(0)
    xs = [v["x"] for v in verts]
    ys = [v["y"] for v in verts]
    assert min(xs) == 100.0 and max(xs) == 110.0
    assert min(ys) == 50.0 and max(ys) == 60.0


def test_smooth_polygon_doubles_vertices() -> None:
    tools.smooth_polygon(polygon_index=0)
    assert len(tools.get_vertices(0)) == 8


# ---- collection management ------------------------------------------------


def test_add_polygon_appends_and_lists() -> None:
    ring = [[100.0, 100.0], [110.0, 100.0], [110.0, 110.0], [100.0, 110.0]]
    out = tools.add_polygon(ring, name="second")
    assert out["ok"] is True
    assert out["index"] == 1
    assert out["total"] == 2
    items = tools.list_polygons()
    assert items[1]["name"] == "second"


def test_remove_polygon_drops_one() -> None:
    ring = [[100.0, 100.0], [110.0, 100.0], [110.0, 110.0], [100.0, 110.0]]
    tools.add_polygon(ring, name="second")
    tools.remove_polygon(0)
    items = tools.list_polygons()
    assert len(items) == 1
    assert items[0]["name"] == "second"


def test_move_vertex_targets_second_polygon() -> None:
    ring = [[100.0, 100.0], [110.0, 100.0], [110.0, 110.0], [100.0, 110.0]]
    tools.add_polygon(ring)
    tools.move_vertex(0, 1.0, 1.0, polygon_index=1)
    assert tools.get_vertices(1)[0] == {"index": 0, "x": 101.0, "y": 101.0}
    # First untouched
    assert tools.get_vertices(0)[0] == {"index": 0, "x": 0.0, "y": 0.0}


# ---- history --------------------------------------------------------------


def test_undo_redo_roundtrip() -> None:
    tools.move_vertex(0, 1.0, 1.0, polygon_index=0)
    tools.undo()
    assert tools.get_vertices(0)[0] == {"index": 0, "x": 0.0, "y": 0.0}
    tools.redo()
    assert tools.get_vertices(0)[0] == {"index": 0, "x": 1.0, "y": 1.0}


def test_undo_add_polygon_removes_it() -> None:
    ring = [[100.0, 100.0], [110.0, 100.0], [110.0, 110.0], [100.0, 110.0]]
    tools.add_polygon(ring)
    assert len(tools.list_polygons()) == 2
    tools.undo()
    assert len(tools.list_polygons()) == 1


def test_undo_on_fresh_state_raises() -> None:
    with pytest.raises(StateError):
        tools.undo()


def test_get_state_returns_singleton() -> None:
    a = tools.get_state()
    b = tools.get_state()
    assert a is b
    assert isinstance(a, PolygonState)
