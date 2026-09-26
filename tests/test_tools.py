"""Tests for the MCP tools defined in glacier_mcp.server.

These call the tool functions directly, without going through the HTTP
transport, so they are fast and dependency-free. A separate test would
be needed to verify the wire protocol itself.
"""

from __future__ import annotations

import pytest
from shapely.geometry import Polygon

from glacier_mcp import tools
from glacier_mcp.state import PolygonState


def _reset_state(square_side: float = 10.0) -> None:
    """Replace the shared state with a fresh square before each test."""
    state = tools.get_state()
    poly = Polygon(
        [
            (0.0, 0.0),
            (square_side, 0.0),
            (square_side, square_side),
            (0.0, square_side),
        ]
    )
    # Use replace() so undo history is not polluted by test setup.
    state._polygon = poly  # noqa: SLF001 - test-only reset
    state._undo.clear()  # noqa: SLF001
    state._redo.clear()  # noqa: SLF001


def setup_function(_func) -> None:  # noqa: ANN001 - pytest hook
    _reset_state()


def test_get_vertices_returns_indices() -> None:
    verts = tools.get_vertices()
    assert [v["index"] for v in verts] == [0, 1, 2, 3]
    assert verts[0] == {"index": 0, "x": 0.0, "y": 0.0}


def test_get_polygon_geojson_shape() -> None:
    gj = tools.get_polygon()
    assert gj["type"] == "Feature"
    assert gj["geometry"]["type"] == "Polygon"
    ring = gj["geometry"]["coordinates"][0]
    # Closed ring: first == last
    assert ring[0] == ring[-1]


def test_move_vertex_returns_new_vertices() -> None:
    out = tools.move_vertex(0, 1.0, 2.0)
    assert out["ok"] is True
    assert out["vertices"][0] == {"index": 0, "x": 1.0, "y": 2.0}


def test_add_vertex_inserts_before_index() -> None:
    out = tools.add_vertex(1, 5.0, -1.0)
    assert out["ok"] is True
    assert out["vertices"][1] == {"index": 1, "x": 5.0, "y": -1.0}


def test_delete_vertex_refuses_when_only_three_left() -> None:
    _reset_state()
    tools.delete_vertex(0)  # now 3 vertices
    with pytest.raises(Exception):
        tools.delete_vertex(0)


def test_translate_polygon_moves_all() -> None:
    out = tools.translate_polygon(100.0, 50.0)
    assert out["ok"] is True
    verts = tools.get_vertices()
    xs = [v["x"] for v in verts]
    ys = [v["y"] for v in verts]
    assert min(xs) == 100.0 and max(xs) == 110.0
    assert min(ys) == 50.0 and max(ys) == 60.0


def test_smooth_polygon_doubles_vertices() -> None:
    tools.smooth_polygon()
    assert len(tools.get_vertices()) == 8


def test_undo_redo_roundtrip() -> None:
    tools.move_vertex(0, 1.0, 1.0)
    tools.undo()
    assert tools.get_vertices()[0] == {"index": 0, "x": 0.0, "y": 0.0}
    tools.redo()
    assert tools.get_vertices()[0] == {"index": 0, "x": 1.0, "y": 1.0}


def test_undo_on_fresh_state_raises() -> None:
    with pytest.raises(Exception):
        tools.undo()


def test_get_state_returns_singleton() -> None:
    a = tools.get_state()
    b = tools.get_state()
    assert a is b
    assert isinstance(a, PolygonState)
